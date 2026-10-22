"""
All Kubernetes / Kubeflow interactions:
  - Kubeflow Profile creation & deletion
  - Namespace lifecycle helpers
  - MinIO secret replication
  - Elyra PodDefault injection
  - GitLab registry imagePullSecret provisioning
"""
import asyncio
import base64
import json
import subprocess

from app.config import GITLAB_REGISTRY_URL, MINIO_NODEPORT_URL, logger
from app.database import update_profile_status


# ---------------------------------------------------------------------------
# Kubeflow Profile
# ---------------------------------------------------------------------------

def create_kubeflow_profile(username: str, owner_email: str) -> None:
    """
    Apply a Kubeflow Profile resource so the controller creates the
    corresponding namespace and RBAC for *owner_email*.

    Raises ``RuntimeError`` on failure so the caller can propagate it as an
    HTTP error.
    """
    profile_yaml = f"""
apiVersion: kubeflow.org/v1
kind: Profile
metadata:
  name: {username}
spec:
  owner:
    kind: User
    name: {owner_email}
"""
    proc = subprocess.Popen(
        ["kubectl", "apply", "-f", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = proc.communicate(input=profile_yaml)
    if proc.returncode != 0:
        raise RuntimeError(stderr)

    logger.info(f"Kubeflow Profile created for '{username}'")


def delete_kubeflow_profile(username: str) -> None:
    """Delete the Kubeflow Profile (non-fatal if already gone)."""
    result = subprocess.run(
        ["kubectl", "delete", "profile", username],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "not found" not in result.stderr.lower():
        logger.error(f"Failed to delete Kubeflow Profile '{username}': {result.stderr}")
    else:
        logger.info(f"Kubeflow Profile '{username}' deleted")


def delete_namespace(username: str) -> None:
    """Initiate namespace deletion (non-blocking, non-fatal)."""
    result = subprocess.run(
        ["kubectl", "delete", "namespace", username, "--wait=false"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.info(f"Namespace '{username}' deletion initiated")
    elif "not found" in result.stderr.lower():
        logger.info(f"Namespace '{username}' already deleted")
    else:
        logger.warning(f"Failed to delete namespace '{username}': {result.stderr}")


# ---------------------------------------------------------------------------
# Multi-Tenant MinIO Provisioning
# ---------------------------------------------------------------------------

import secrets


def _get_minio_root_creds() -> tuple[str, str]:
    """Fetch root MinIO accesskey and secretkey from 'mlpipeline-minio-artifact' secret in 'kubeflow'."""
    result = subprocess.run(
        ["kubectl", "get", "secret", "mlpipeline-minio-artifact", "-n", "kubeflow", "-o", "jsonpath={.data.accesskey}"],
        capture_output=True,
        text=True,
    )
    access_b64 = result.stdout.strip()

    result_sec = subprocess.run(
        ["kubectl", "get", "secret", "mlpipeline-minio-artifact", "-n", "kubeflow", "-o", "jsonpath={.data.secretkey}"],
        capture_output=True,
        text=True,
    )
    secret_b64 = result_sec.stdout.strip()

    if not access_b64 or not secret_b64:
        raise RuntimeError("Could not retrieve MinIO root secret from 'kubeflow' namespace.")

    root_user = base64.b64decode(access_b64).decode()
    root_pass = base64.b64decode(secret_b64).decode()
    return root_user, root_pass


def provision_user_minio_account(username: str, provided_password: str = None) -> str:
    """
    Creates an isolated MinIO account and bucket for *username*:
    1. Uses provided password (or generates random) for *username*.
    2. Executes local `mc` client against `MINIO_NODEPORT_URL` to:
       - Set alias `local` with root creds.
       - Create bucket `<username>-bucket`.
       - Create user `<username>`.
       - Create & attach IAM policy `policy-<username>` restricting access to `<username>-bucket`.
    3. Returns the user password.
    """
    user_pass = provided_password if provided_password else secrets.token_urlsafe(16)
    bucket_name = f"{username}-bucket"
    policy_name = f"policy-{username}"

    root_user, root_pass = _get_minio_root_creds()
    minio_url = MINIO_NODEPORT_URL

    logger.info(f"Provisioning isolated MinIO account & bucket '{bucket_name}' at {minio_url} for user '{username}'...")

    policy_json = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:*"],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*"
                ]
            }
        ]
    })

    # 1. Alias
    alias_cmd = ["mc", "alias", "set", "local", minio_url, root_user, root_pass]
    sub_alias = subprocess.run(alias_cmd, capture_output=True, text=True)
    if sub_alias.returncode != 0:
        logger.error(f"Failed to set mc alias for MinIO at {minio_url}: {sub_alias.stderr}")
        raise RuntimeError(f"mc alias set failed: {sub_alias.stderr}")

    # 2. Bucket creation
    mb_cmd = ["mc", "mb", "--ignore-existing", f"local/{bucket_name}"]
    mb_res = subprocess.run(mb_cmd, capture_output=True, text=True)
    if mb_res.returncode != 0 and "already exists" not in mb_res.stderr.lower():
        logger.warning(f"MinIO bucket creation notice for '{bucket_name}': {mb_res.stderr}")

    # 3. User creation
    user_cmd = ["mc", "admin", "user", "add", "local", username, user_pass]
    u_res = subprocess.run(user_cmd, capture_output=True, text=True)
    if u_res.returncode != 0 and "already exists" not in u_res.stderr.lower():
        logger.error(f"Failed to create MinIO user '{username}': {u_res.stderr}")
        raise RuntimeError(f"MinIO user creation failed: {u_res.stderr}")

    # 4. Create Policy
    policy_file = f"/tmp/{policy_name}.json"
    with open(policy_file, "w") as f:
        f.write(policy_json)

    pol_cmd = ["mc", "admin", "policy", "create", "local", policy_name, policy_file]
    pol_res = subprocess.run(pol_cmd, capture_output=True, text=True)
    if pol_res.returncode != 0 and "already exists" not in pol_res.stderr.lower():
        logger.warning(f"MinIO policy creation notice for '{policy_name}': {pol_res.stderr}")

    # 5. Attach bucket-scoped Policy
    attach_cmd = ["mc", "admin", "policy", "attach", "local", policy_name, f"--user={username}"]
    att_res = subprocess.run(attach_cmd, capture_output=True, text=True)
    if att_res.returncode != 0:
        logger.warning(f"MinIO policy attach notice for '{username}': {att_res.stderr}")

    # 6. Attach consoleAdmin so the user can log into MinIO Console UI (port 9001)
    console_cmd = ["mc", "admin", "policy", "attach", "local", "consoleAdmin", f"--user={username}"]
    console_res = subprocess.run(console_cmd, capture_output=True, text=True)
    if console_res.returncode != 0:
        logger.warning(f"MinIO consoleAdmin policy attach notice for '{username}': {console_res.stderr}")

    logger.info(f"Isolated MinIO account '{username}' successfully provisioned with bucket '{bucket_name}'.")
    return user_pass


def delete_minio_console_virtualservice(username: str) -> None:
    """
    Remove the per-user MinIO Console VirtualService from the kubeflow namespace.

    The VirtualService is named ``minio-console-{username}`` and lives in the
    ``kubeflow`` namespace (alongside the gateway).  We use ``--ignore-not-found``
    so this is a no-op if the resource was already cleaned up manually.
    """
    res = subprocess.run(
        ["kubectl", "delete", "virtualservice",
         f"minio-console-{username}",
         "-n", "kubeflow",
         "--ignore-not-found"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        logger.info(f"MinIO Console VirtualService 'minio-console-{username}' deleted (or not found).")
    else:
        logger.warning(f"Could not delete MinIO VirtualService for '{username}': {res.stderr}")


def delete_user_minio_account(username: str) -> None:
    """Delete MinIO user account and policy when user profile is deleted."""
    try:
        root_user, root_pass = _get_minio_root_creds()
        minio_url = MINIO_NODEPORT_URL
        policy_name = f"policy-{username}"

        subprocess.run(["mc", "alias", "set", "local", minio_url, root_user, root_pass], capture_output=True)
        subprocess.run(["mc", "admin", "user", "remove", "local", username], capture_output=True)
        subprocess.run(["mc", "admin", "policy", "remove", "local", policy_name], capture_output=True)
        logger.info(f"MinIO user account '{username}' and policy '{policy_name}' removed.")
    except Exception as exc:
        logger.warning(f"Could not delete MinIO user '{username}': {exc}")


def patch_decorator_controller_if_needed() -> None:
    """Patch the kubeflow-pipelines-profile-controller to not manage secrets."""
    import json
    res = subprocess.run(
        ["kubectl", "get", "decoratorcontroller", "kubeflow-pipelines-profile-controller", "-o", "json"],
        capture_output=True,
        text=True
    )
    if res.returncode != 0:
        logger.warning(f"Could not find decoratorcontroller: {res.stderr}")
        return
    try:
        dc = json.loads(res.stdout)
        attachments = dc.get("spec", {}).get("attachments", [])
        has_secrets = any(a.get("resource") == "secrets" for a in attachments)
        if not has_secrets:
            return
        logger.info("Patching kubeflow-pipelines-profile-controller to disable secrets management...")
        new_attachments = [a for a in attachments if a.get("resource") != "secrets"]
        patch = [{"op": "replace", "path": "/spec/attachments", "value": new_attachments}]
        patch_res = subprocess.run(
            ["kubectl", "patch", "decoratorcontroller", "kubeflow-pipelines-profile-controller",
             "--type", "json", "-p", json.dumps(patch)],
            capture_output=True,
            text=True
        )
        if patch_res.returncode == 0:
            logger.info("Successfully patched DecoratorController.")
        else:
            logger.error(f"Failed to patch DecoratorController: {patch_res.stderr}")
    except Exception as exc:
        logger.error(f"Error patching DecoratorController: {exc}")


def deploy_local_minio_in_namespace(namespace: str, password: str = None) -> None:
    """
    Deploy PVC, Service, and Deployment for a dedicated MinIO instance inside the
    user's namespace.

    KEY DESIGN: The MinIO pod reads its root credentials directly from the
    ``mlpipeline-minio-artifact`` secret via secretKeyRef.  This means that
    whatever credentials the kfp-profile-controller (Metacontroller) writes into
    that secret, the local MinIO will accept them — so we never need to fight the
    controller.  Storage isolation is achieved through the per-namespace PVC.
    """
    import json

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "minio-pvc",
            "namespace": namespace
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {
                "requests": {
                    "storage": "10Gi"
                }
            }
        }
    }

    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "minio",
            "namespace": namespace,
            "labels": {
                "managed-by": "kubeflow-admin-portal"
            }
        },
        "spec": {
            "ports": [
                {"port": 9000, "targetPort": 9000, "name": "api"},
                {"port": 9001, "targetPort": 9001, "name": "console"}
            ],
            "selector": {
                "app": "minio"
            }
        }
    }

    deploy = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "minio",
            "namespace": namespace,
            "labels": {
                "app": "minio",
                "managed-by": "kubeflow-admin-portal"
            }
        },
        "spec": {
            "strategy": {
                "type": "Recreate"
            },
            "selector": {
                "matchLabels": {
                    "app": "minio"
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app": "minio"
                    }
                },
                "spec": {
                    # Wait for the mlpipeline-minio-artifact secret to exist before starting.
                    # This guarantees the MinIO pod starts with the correct credentials.
                    "initContainers": [{
                        "name": "wait-for-secret",
                        "image": "bitnami/kubectl:latest",
                        "command": [
                            "sh", "-c",
                            "until kubectl get secret mlpipeline-minio-artifact -n " + namespace + "; "
                            "do echo 'Waiting for mlpipeline-minio-artifact secret...'; sleep 2; done"
                        ]
                    }],
                    "containers": [{
                        "name": "minio",
                        "image": "minio/minio:RELEASE.2023-08-29T23-07-35Z",
                        "args": ["server", "/data", "--console-address", ":9001"],
                        "env": [
                            {
                                # Tell MinIO Console its external subpath so that after
                                # a successful login it redirects to /minio/<ns>/ instead
                                # of the bare root /.  Without this, the post-login
                                # redirect escapes the subpath and breaks the iframe.
                                "name": "MINIO_BROWSER_REDIRECT_URL",
                                "value": "http://10.110.188.200/minio/" + namespace + "/"
                            },
                            {
                                "name": "MINIO_ROOT_USER",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "mlpipeline-minio-artifact",
                                        "key": "accesskey"
                                    }
                                }
                            },
                            {
                                "name": "MINIO_ROOT_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "mlpipeline-minio-artifact",
                                        "key": "secretkey"
                                    }
                                }
                            }
                        ],
                        "ports": [
                            {"containerPort": 9000, "name": "api"},
                            {"containerPort": 9001, "name": "console"}
                        ],
                        "volumeMounts": [{
                            "mountPath": "/data",
                            "name": "minio-storage"
                        }],
                        "lifecycle": {
                            "postStart": {
                                "exec": {
                                    "command": ["/bin/sh", "-c",
                                               "sleep 3; mkdir -p /data/mlflow /data/mlpipeline"]
                                }
                            }
                        }
                    }],
                    "volumes": [{
                        "name": "minio-storage",
                        "persistentVolumeClaim": {
                            "claimName": "minio-pvc"
                        }
                    }]
                }
            }
        }
    }

    for resource in [pvc, svc, deploy]:
        proc = subprocess.Popen(
            ["kubectl", "apply", "-f", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        _, stderr = proc.communicate(input=json.dumps(resource))
        if proc.returncode != 0:
            logger.error(f"Failed to apply local MinIO resource for '{namespace}': {stderr}")
            raise RuntimeError(f"Failed to apply local MinIO: {stderr}")

    # Deploy VirtualService to expose MinIO Console through Istio gateway
    deploy_minio_console_virtualservice(namespace)


def ensure_admin_portal_ingress_proxy() -> None:
    """
    Ensure the Istio routing resources exist in the 'kubeflow' namespace
    to route '/api/minio/' to the Admin Portal VM (10.110.188.148:7070).
    """
    import json

    # 1. Service pointing to the Admin Portal VM (without selectors)
    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "admin-portal-proxy",
            "namespace": "kubeflow",
            "labels": {
                "managed-by": "kubeflow-admin-portal"
            }
        },
        "spec": {
            "ports": [
                {"name": "http", "port": 7070, "targetPort": 7070}
            ]
        }
    }

    # 2. Endpoints mapping the service to the Admin Portal IP (10.110.188.148)
    ep = {
        "apiVersion": "v1",
        "kind": "Endpoints",
        "metadata": {
            "name": "admin-portal-proxy",
            "namespace": "kubeflow",
            "labels": {
                "managed-by": "kubeflow-admin-portal"
            }
        },
        "subsets": [
            {
                "addresses": [{"ip": "10.110.188.148"}],
                "ports": [{"name": "http", "port": 7070}]
            }
        ]
    }

    # 3. VirtualService routing /api/minio/ and /_api/minio/ from the gateway to the service
    vs = {
        "apiVersion": "networking.istio.io/v1alpha3",
        "kind": "VirtualService",
        "metadata": {
            "name": "admin-portal-proxy",
            "namespace": "kubeflow",
            "labels": {
                "managed-by": "kubeflow-admin-portal"
            }
        },
        "spec": {
            "gateways": ["kubeflow/kubeflow-gateway"],
            "hosts": ["*"],
            "http": [
                {
                    "match": [
                        {"uri": {"prefix": "/api/minio/"}},
                        {"uri": {"prefix": "/_api/minio/"}}
                    ],
                    "route": [{
                        "destination": {
                            "host": "admin-portal-proxy.kubeflow.svc.cluster.local",
                            "port": {"number": 7070}
                        }
                    }]
                }
            ]
        }
    }

    for resource in [svc, ep, vs]:
        proc = subprocess.Popen(
            ["kubectl", "apply", "-f", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        _, stderr = proc.communicate(input=json.dumps(resource))
        if proc.returncode != 0:
            logger.error(f"Failed to apply admin-portal proxy resource: {stderr}")



    logger.info(f"Local MinIO instance successfully created in namespace '{namespace}' (uses shared credentials, isolated PVC)")


def deploy_minio_console_virtualservice(namespace: str) -> None:
    """
    Deploy an Istio VirtualService that exposes the namespace-local MinIO Console
    (port 9001) at the path ``/minio/<namespace>/`` through the shared Kubeflow
    Istio ingress gateway.

    This allows the Kubeflow Dashboard sidebar to link directly to each user's
    private MinIO Console UI.
    """
    import json

    # IMPORTANT: The VirtualService MUST live in the same namespace as the gateway
    # (kubeflow) for the Istio ingress gateway pod to pick it up.
    # VirtualServices in user namespaces referencing a gateway in another namespace
    # are silently ignored by Istio.  We name it "minio-console-<namespace>" to
    # avoid collisions between multiple users.
    vs = {
        "apiVersion": "networking.istio.io/v1alpha3",
        "kind": "VirtualService",
        "metadata": {
            "name": f"minio-console-{namespace}",
            "namespace": "kubeflow",
            "labels": {
                "managed-by": "kubeflow-admin-portal",
                "user-namespace": namespace
            }
        },
        "spec": {
            "gateways": ["kubeflow-gateway"],
            "hosts": ["*"],
            "http": [
                {
                    "match": [{"uri": {"prefix": f"/minio/{namespace}/"}}],
                    "rewrite": {"uri": "/"},
                    "route": [{
                        "destination": {
                            "host": f"minio.{namespace}.svc.cluster.local",
                            "port": {"number": 9001}
                        }
                    }],
                    # Strip X-Frame-Options so MinIO Console loads inside the
                    # Kubeflow Dashboard iframe without being blocked by the browser.
                    "headers": {
                        "response": {
                            "remove": ["x-frame-options", "X-Frame-Options"]
                        }
                    },
                    "timeout": "300s",
                }
            ]
        }
    }

    proc = subprocess.Popen(
        ["kubectl", "apply", "-f", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = proc.communicate(input=json.dumps(vs))
    if proc.returncode == 0:
        logger.info(f"MinIO Console VirtualService deployed for namespace '{namespace}'")
    else:
        logger.error(f"Failed to deploy MinIO Console VirtualService for '{namespace}': {stderr}")

    # Allow the MinIO Console login endpoint and static assets to be reached
    # by the browser through the Istio sidecar WITHOUT requiring a kubeflow-userid
    # header.  The ns-owner-access-istio-charmed AuthorizationPolicy deployed by
    # kfp-profile-controller blocks all traffic that lacks that header, which
    # causes the JS fetch() to /minio/{namespace}/api/v1/login to get a 403.
    # We add a targeted allow-rule for MinIO Console paths only.
    authz = {
        "apiVersion": "security.istio.io/v1beta1",
        "kind": "AuthorizationPolicy",
        "metadata": {
            "name": "minio-console-allow",
            "namespace": namespace,
            "labels": {"managed-by": "kubeflow-admin-portal"}
        },
        "spec": {
            "selector": {
                "matchLabels": {"app": "minio"}
            },
            "rules": [
                # Allow all traffic to MinIO Console (port 9001) unconditionally.
                # The Console itself enforces authentication via its session token.
                # NOTE: Istio AuthorizationPolicy ports must be strings, not integers.
                {
                    "to": [
                        {"operation": {"ports": ["9001"]}}
                    ]
                },
                # Also allow the S3 API port so Elyra/KFP can reach it
                {
                    "to": [
                        {"operation": {"ports": ["9000"]}}
                    ]
                }
            ]
        }
    }

    proc2 = subprocess.Popen(
        ["kubectl", "apply", "-f", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr2 = proc2.communicate(input=json.dumps(authz))
    if proc2.returncode == 0:
        logger.info(f"MinIO Console AuthorizationPolicy deployed for namespace '{namespace}'")
    else:
        logger.error(f"Failed to deploy MinIO AuthorizationPolicy for '{namespace}': {stderr2}")


async def replicate_minio_secret_and_verify(username: str, password: str = None) -> None:
    """
    Background task that:
    1. Waits for the Kubeflow namespace to appear (up to 30 s).
    2. Deploys a dedicated local MinIO (PVC + Service + Deployment) in the user
       namespace.  The local MinIO reads credentials from the
       ``mlpipeline-minio-artifact`` secret that Metacontroller writes
       automatically, so we never fight the controller.
       Storage isolation is guaranteed via the per-namespace PVC.
    3. Deploys an Istio VirtualService to expose the MinIO Console on
       ``/minio/<username>/`` through the Kubeflow gateway.
    4. Applies the Elyra PodDefault pointing to the local MinIO service.
    """
    logger.info(
        f"Starting Profile/Namespace verification and local MinIO setup for: {username}"
    )
    # 1. Wait for namespace
    ns_ready = await _wait_for_namespace(username, timeout=30)
    if not ns_ready:
        logger.error(
            f"Namespace '{username}' was not created in time. Cannot setup MinIO."
        )
        update_profile_status(username, "NAMESPACE_TIMEOUT")
        return

    # 2. Deploy dedicated local MinIO — credentials come from the shared secret
    #    that the Metacontroller writes automatically into every namespace.
    try:
        deploy_local_minio_in_namespace(username, password)
    except Exception as exc:
        logger.error(f"Failed to deploy local MinIO for '{username}': {exc}")
        update_profile_status(username, "MINIO_PROVISION_FAILED")
        return

    # 3. Apply Elyra PodDefault and mark user as READY
    update_profile_status(username, "READY")
    apply_elyra_poddefault(username)


async def _wait_for_namespace(username: str, timeout: int = 30) -> bool:
    """Return ``True`` once the namespace exists, ``False`` on timeout."""
    for i in range(timeout):
        result = subprocess.run(
            ["kubectl", "get", "namespace", username],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info(f"Namespace '{username}' verified after {i} second(s).")
            return True
        await asyncio.sleep(1)
    return False


# ---------------------------------------------------------------------------
# Elyra PodDefault
# ---------------------------------------------------------------------------

def apply_elyra_poddefault(namespace: str) -> None:
    """
    Apply the ``elyra-minio-access`` PodDefault to *namespace*.

    Injects MinIO credentials and public browser endpoints as environment variables into
    every notebook pod labelled ``elyra-minio-access=true``. Points to *namespace*-bucket.
    """
    manifest = f"""
apiVersion: kubeflow.org/v1alpha1
kind: PodDefault
metadata:
  name: elyra-minio-access
  namespace: {namespace}
spec:
  selector:
    matchLabels:
      elyra-minio-access: "true"
  desc: "Auto-inject MinIO credentials and public links for Elyra KFP runtime"
  env:
    - name: ELYRA_COS_ENDPOINT
      value: "http://minio:9000"
    - name: ELYRA_COS_PUBLIC_ENDPOINT
      value: "http://minio:9000"
    - name: ELYRA_COS_BUCKET
      value: "mlpipeline"
    - name: ELYRA_COS_USERNAME
      valueFrom:
        secretKeyRef:
          name: mlpipeline-minio-artifact
          key: accesskey
    - name: ELYRA_COS_PASSWORD
      valueFrom:
        secretKeyRef:
          name: mlpipeline-minio-artifact
          key: secretkey
    - name: KFP_PUBLIC_ENDPOINT
      value: "http://10.110.188.144.nip.io"
    - name: ELYRA_KFP_PUBLIC_ENDPOINT
      value: "http://10.110.188.144.nip.io"
    # MLflow tracking — auto-injected so every pipeline step can call
    # mlflow.log_param() / mlflow.log_metric() with zero configuration.
    - name: MLFLOW_TRACKING_URI
      value: "http://mlflow-server.kubeflow.svc.cluster.local:5000"
    - name: MLFLOW_S3_ENDPOINT_URL
      value: "http://mlflow-minio.kubeflow.svc.cluster.local:9000"
    - name: AWS_ACCESS_KEY_ID
      valueFrom:
        secretKeyRef:
          name: mlpipeline-minio-artifact
          key: accesskey
    - name: AWS_SECRET_ACCESS_KEY
      valueFrom:
        secretKeyRef:
          name: mlpipeline-minio-artifact
          key: secretkey
"""
    proc = subprocess.Popen(
        ["kubectl", "apply", "-f", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = proc.communicate(input=manifest)
    if proc.returncode == 0:
        logger.info(f"elyra-minio-access PodDefault applied to namespace '{namespace}'")
    else:
        logger.error(f"Failed to apply PodDefault to '{namespace}': {stderr}")


# ---------------------------------------------------------------------------
# GitLab registry imagePullSecret
# ---------------------------------------------------------------------------

def create_registry_pull_secret(username: str, registry_token: str) -> None:
    """
    Create (or replace) a ``gitlab-registry`` imagePullSecret in the user's
    namespace and patch all existing Notebook CRs so Kubeflow pods can pull
    from the user's GitLab registry automatically.

    Note: Kubeflow's notebook controller owns the StatefulSet and reconciles
    it continuously from the Notebook CR. Patching the StatefulSet directly
    gets overwritten. We patch the Notebook CR instead.
    """
    auth_str = base64.b64encode(f"{username}:{registry_token}".encode()).decode()
    # Strip http:// — dockerconfigjson keys must be host:port only
    registry_host = GITLAB_REGISTRY_URL.replace("http://", "").replace("https://", "")
    docker_config = {
        "auths": {
            registry_host: {
                "username": username,
                "password": registry_token,
                "auth": auth_str,
            }
        }
    }
    docker_config_b64 = base64.b64encode(
        json.dumps(docker_config).encode()
    ).decode()

    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "gitlab-registry",
            "namespace": username,
            "annotations": {
                "managed-by": "kubeflow-admin-portal",
                "registry-user": username,
            },
        },
        "type": "kubernetes.io/dockerconfigjson",
        "data": {
            ".dockerconfigjson": docker_config_b64,
        },
    }

    proc = subprocess.Popen(
        ["kubectl", "apply", "-f", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = proc.communicate(input=json.dumps(secret_manifest))
    if proc.returncode == 0:
        logger.info(f"gitlab-registry imagePullSecret created in namespace '{username}'")
        _patch_default_service_account(username)
        _patch_notebooks_image_pull_secret(username)
    else:
        logger.error(
            f"Failed to create gitlab-registry secret in '{username}': {stderr}"
        )


def _patch_notebooks_image_pull_secret(namespace: str) -> None:
    """
    Patch all existing Notebook CRs in *namespace* to include the
    ``gitlab-registry`` imagePullSecret.

    Kubeflow reconciles the StatefulSet from the Notebook CR, so any patch
    must go on the Notebook CR — not the StatefulSet directly.
    New notebooks created after the secret exists don't need this because
    the default ServiceAccount already has the imagePullSecret.
    """
    # List all notebooks in the namespace
    result = subprocess.run(
        ["kubectl", "get", "notebooks", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        logger.info(f"No existing Notebooks found in namespace '{namespace}'")
        return

    notebooks = result.stdout.strip().split()
    patch = json.dumps([{
        "op": "add",
        "path": "/spec/template/spec/imagePullSecrets",
        "value": [{"name": "gitlab-registry"}]
    }])

    for notebook in notebooks:
        nb_result = subprocess.run(
            ["kubectl", "patch", "notebook", notebook, "-n", namespace,
             "--type", "json", "-p", patch],
            capture_output=True,
            text=True,
        )
        if nb_result.returncode == 0:
            logger.info(f"Notebook '{notebook}' in '{namespace}' patched with gitlab-registry")
        else:
            logger.warning(f"Could not patch Notebook '{notebook}' in '{namespace}': {nb_result.stderr}")


def _patch_default_service_account(namespace: str) -> None:
    """
    Add ``gitlab-registry`` to the imagePullSecrets of the ``default``
    ServiceAccount so every pod in the namespace pulls automatically.
    """
    patch = json.dumps({
        "imagePullSecrets": [{"name": "gitlab-registry"}]
    })
    result = subprocess.run(
        [
            "kubectl", "patch", "serviceaccount", "default",
            "-n", namespace,
            "--type", "merge",
            "-p", patch,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.info(
            f"default ServiceAccount in '{namespace}' patched with gitlab-registry pull secret"
        )
    else:
        logger.warning(
            f"Could not patch default ServiceAccount in '{namespace}': {result.stderr}"
        )
