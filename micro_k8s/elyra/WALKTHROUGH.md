# Elyra Auto-Config — Beginner Walkthrough

> **Goal:** When an AI/ML engineer opens JupyterLab, a fully-configured Elyra
> pipeline runtime called `default` already exists — no terminal, no typing, no
> kubectl required.  This document explains every step, *why* it exists, and
> *what would break* if you skipped it.

---

## Background — How the pieces connect

Before touching any file, it helps to understand the three systems involved:

| System | What it does in this cluster |
|---|---|
| **Kubeflow Profiles** | Each user gets their own namespace (`la9a`, `kfplead`, …). A Profile is just a Kubernetes object that tells Kubeflow "create a namespace for this person and wire up RBAC". |
| **PodDefault** | A Kubeflow CRD. Think of it as a patch that gets automatically applied to every notebook pod that opts in. It can inject environment variables, volumes, or secrets — without the user knowing. |
| **Elyra** | A JupyterLab extension that adds a visual pipeline editor. Before it can submit a pipeline to Kubeflow Pipelines it needs a "runtime configuration" — essentially a JSON file that tells it where the KFP API is, which MinIO bucket to use, and what credentials to use. We are going to write that file automatically. |

The flow we are building:

```
Admin creates user  →  app.py creates Profile + namespace
                    →  background task replicates MinIO secret
                    →  [NEW] background task applies PodDefault
                    →  user spawns notebook (elyra:v2 image)
                    →  PodDefault injects MinIO env vars into pod
                    →  s6 init script reads env vars, writes default.json
                    →  user opens JupyterLab → runtime already configured
```

---


## Step 1 — Create `setup-elyra-runtime.sh`

**File:** `micro_k8s/elyra/setup-elyra-runtime.sh`

### What is this?

This is a small shell script that runs *inside the notebook container* every time
the container starts, before JupyterLab opens.  Its only job is to write Elyra's
runtime configuration JSON file if it does not already exist.

### Why do we need it?

Elyra stores its runtime configurations as plain JSON files on disk under
`~/.local/share/jupyter/metadata/runtimes/`.  There is no API to pre-seed them
from outside the container — the only way to get the file there is to write it
from inside, at startup.

### Why the `if [ ! -f ]` guard?

Idempotency.  If the user manually edits the runtime config (changes the bucket
name, for example), we must not overwrite their change on every restart.  We
write the file exactly once — the first time the container ever starts.

### Where do `ELYRA_COS_USERNAME` etc. come from?

They come from the PodDefault we create in Step 4.  The PodDefault tells
Kubernetes to inject those environment variables into the pod before the
container process starts.  So by the time this script runs, the variables are
already present in the shell environment.

### How does the script know the user's namespace?

Kubernetes automatically mounts a file at
`/var/run/secrets/kubernetes.io/serviceaccount/namespace` inside every pod.
It contains exactly one line: the name of the namespace the pod is running in
(e.g. `la9a`).  We `cat` that file — no hardcoding required.

```bash
#!/bin/bash
set -e

RUNTIME_DIR="$HOME/.local/share/jupyter/metadata/runtimes"
RUNTIME_FILE="$RUNTIME_DIR/default.json"

# Only write the file if it doesn't already exist
if [ ! -f "$RUNTIME_FILE" ]; then
  NAMESPACE=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
  mkdir -p "$RUNTIME_DIR"

  cat > "$RUNTIME_FILE" <<EOF
{
  "schema_name": "kfp",
  "display_name": "default",
  "metadata": {
    "api_endpoint": "http://ml-pipeline.kubeflow.svc.cluster.local:8888",
    "user_namespace": "${NAMESPACE}",
    "engine": "Argo",
    "auth_type": "KUBERNETES_SERVICE_ACCOUNT_TOKEN",
    "cos_endpoint": "${ELYRA_COS_ENDPOINT}",
    "cos_bucket": "${ELYRA_COS_BUCKET}",
    "cos_username": "${ELYRA_COS_USERNAME}",
    "cos_password": "${ELYRA_COS_PASSWORD}",
    "cos_auth_type": "USER_CREDENTIALS"
  }
}
EOF
fi
```

---


## Step 2 — Create the `Dockerfile`

**File:** `micro_k8s/elyra/Dockerfile`

### What is this?

A Dockerfile describes how to build a Docker image layer by layer.  Our image
is `abidology/elyra`.  Version 1 (`v1`) already exists and is used in
production.  We are adding two new layers on top of it to produce `v2`.

### Why not change the base image?

The base `ghcr.io/kubeflow/kubeflow/notebook-servers/jupyter:v1.8.0` is the
Charmed Kubeflow official notebook base.  It ships with the Juju agent, the
s6-overlay init system, and all the wiring for JupyterLab.  Changing it would
break compatibility with the rest of the Kubeflow charm stack.

### What is s6-overlay?

s6-overlay is a lightweight process supervisor (init system) baked into the
Kubeflow notebook base image.  Instead of a single `ENTRYPOINT` script,
s6-overlay starts JupyterLab through a chain of "one-shot" scripts located in
`/etc/cont-init.d/`.  Scripts there run in alphabetical order, once, before the
main process starts.  We register our script as `02-setup-elyra` so it runs
early in that chain — before JupyterLab serves any requests.

### Why `exec` in the cont-init wrapper?

s6-overlay v1 expects cont-init scripts to exit cleanly.  Using `exec` replaces
the shell process with our script, which keeps the process tree clean and ensures
the exit code of our script propagates correctly to s6.

### The v1 → v2 diff (only the new lines):

```dockerfile
FROM ghcr.io/kubeflow/kubeflow/notebook-servers/jupyter:v1.8.0

USER root

RUN python3 -m pip install --upgrade pip

RUN python3 -m pip install \
    "elyra[kfp,kfp-examples,airflow-gitlab]"

RUN jupyter elyra --generate-config

RUN sed -i \
    "s/# c.PipelineProcessorRegistry.runtimes = \[\]/c.PipelineProcessorRegistry.runtimes = \['kfp'\]/g" \
    $(jupyter --config-dir)/jupyter_elyra_config.py

# ── NEW in v2 ─────────────────────────────────────────────────────────────────
COPY setup-elyra-runtime.sh /usr/local/bin/setup-elyra-runtime.sh
RUN chmod +x /usr/local/bin/setup-elyra-runtime.sh

# Register as an s6 cont-init step — runs once per container start,
# before JupyterLab, as the process started by s6-overlay.
RUN mkdir -p /etc/cont-init.d && \
    printf '#!/bin/sh\nexec /usr/local/bin/setup-elyra-runtime.sh\n' \
    > /etc/cont-init.d/02-setup-elyra && \
    chmod +x /etc/cont-init.d/02-setup-elyra
# ──────────────────────────────────────────────────────────────────────────────

USER jovyan

RUN elyra-metadata create component-catalogs \
    --schema_name=elyra-kfp-examples-catalog \
    --display_name="Kubeflow Pipelines examples" \
    --runtime_type=KUBEFLOW_PIPELINES \
    --categories="['examples']"
```

---


## Step 3 — Build and push `abidology/elyra:v2`

### What happens here?

We build the image from the Dockerfile we wrote in Step 2 and push it to Docker
Hub under the `abidology` account so Kubernetes can pull it when a notebook is
spawned.

### Commands to run (on your local machine or the control plane):

```bash
cd /home/ahmed-bensalah/Micro-K8s\ kubeflow/micro_k8s/elyra/

# Build — this will take several minutes because pip installs elyra
docker build -t abidology/elyra:v2 .

# Log in to Docker Hub if not already logged in
docker login

# Push the new image
docker push abidology/elyra:v2
```

### How to verify the build worked before pushing?

Run a quick smoke-test container to confirm the init script is in place and the
JSON template is syntactically valid:

```bash
# Start a throwaway container with fake env vars
docker run --rm \
  -e ELYRA_COS_ENDPOINT="http://fake:9000" \
  -e ELYRA_COS_BUCKET="mlpipeline" \
  -e ELYRA_COS_USERNAME="fakeuser" \
  -e ELYRA_COS_PASSWORD="fakepass" \
  abidology/elyra:v2 \
  bash -c '
    export HOME=/tmp/testhome
    mkdir -p /var/run/secrets/kubernetes.io/serviceaccount
    echo "testnamespace" > /var/run/secrets/kubernetes.io/serviceaccount/namespace
    /usr/local/bin/setup-elyra-runtime.sh
    cat /tmp/testhome/.local/share/jupyter/metadata/runtimes/default.json
  '
```

You should see a fully-populated JSON with no `${...}` placeholders.  If you
see empty values, the env vars are not being passed correctly — fix that before
moving on.

---


## Step 4 — Update `app.py` to apply the PodDefault

**File:** `micro_k8s/ansible/roles/gitlab/files/app.py`

### What is a PodDefault?

A PodDefault is a Kubeflow custom resource (similar to a ConfigMap or Secret)
that lives inside a user's namespace.  When a notebook pod is spawned in that
namespace AND the pod carries a matching label, the Kubeflow admission webhook
automatically patches the pod — injecting whatever the PodDefault specifies
(environment variables, volumes, etc.) — before the container starts.

### Why apply it from `app.py` and not somewhere else?

`app.py` is already the place that creates the Kubeflow Profile and replicates
the MinIO secret for each new user.  It has a background task
(`replicate_minio_secret_and_verify`) that already waits for the namespace to be
ready and the secret to appear.  Adding the PodDefault creation in that same
background task means:

- Zero new infrastructure (no extra controller, no CronJob).
- The PodDefault is created at exactly the right moment — after the secret it
  depends on (`mlpipeline-minio-artifact`) already exists in the namespace.
- The race condition is already handled by the existing retry loop.

### The new function to add (inside `app.py`):

```python
def apply_elyra_poddefault(namespace: str):
    """
    Apply the elyra-minio-access PodDefault to a user namespace.
    This PodDefault injects MinIO credentials as environment variables
    into every notebook pod that carries the label elyra-minio-access=true.
    It must be applied AFTER mlpipeline-minio-artifact secret exists,
    because the PodDefault references that secret via secretKeyRef.
    """
    poddefault_manifest = f"""
apiVersion: kubeflow.org/v1alpha1
kind: PodDefault
metadata:
  name: elyra-minio-access
  namespace: {namespace}
spec:
  selector:
    matchLabels:
      elyra-minio-access: "true"
  desc: "Auto-inject MinIO credentials for Elyra KFP runtime"
  env:
    - name: ELYRA_COS_ENDPOINT
      value: "http://minio.kubeflow.svc.cluster.local:9000"
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
"""
    proc = subprocess.Popen(
        ["kubectl", "apply", "-f", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True
    )
    stdout, stderr = proc.communicate(input=poddefault_manifest)
    if proc.returncode == 0:
        logger.info(f"elyra-minio-access PodDefault applied to namespace '{namespace}'")
    else:
        logger.error(f"Failed to apply PodDefault to '{namespace}': {stderr}")
```

### Where to call it inside the existing background task:

In `replicate_minio_secret_and_verify`, right after the line that calls
`update_db_profile_status(username, "READY")`:

```python
        if apply_proc.returncode == 0:
            logger.info(f"MinIO Secret successfully replicated to '{username}' namespace.")
            update_db_profile_status(username, "READY")
            apply_elyra_poddefault(username)   # ← add this line
```

### Why `kubectl apply` instead of `kubectl create`?

`apply` is idempotent — if the PodDefault already exists (for example because
you ran the portal twice for the same user), it just updates it instead of
failing with "already exists".  `create` would error out on the second run.

---


## Step 5 — Understand what `spawner_ui_config.yaml` controls

Before patching anything it is important to understand what this file is and
why touching it is necessary.

### What is `spawner_ui_config.yaml`?

It is the configuration file for the Kubeflow Notebooks UI (the "Spawn" page
where users pick an image, RAM, CPU, etc.).  Among other things, it controls
the **Configurations** section — the list of PodDefaults that the user can
opt in to when creating a notebook.

The relevant section currently looks like this:

```yaml
configurations:
  readOnly: false
  value:
    - access-ml-pipeline   # ← this is the only one today
```

The `value` list contains the names of PodDefaults that are **pre-selected by
default**.  Because `access-ml-pipeline` is in this list, every notebook pod
automatically carries the label `access-ml-pipeline: "true"`, which causes the
`access-ml-pipeline` PodDefault to inject the KFP service-account token.

### What do we need to add?

```yaml
configurations:
  readOnly: false
  value:
    - access-ml-pipeline
    - elyra-minio-access   # ← add this
```

By adding `elyra-minio-access` here, every notebook pod will also carry the
label `elyra-minio-access: "true"`, causing our new PodDefault to inject the
MinIO environment variables — zero user clicks needed.

### Where does this file live?

It is **not** a plain file on disk.  It lives inside the `jupyter-ui` pod
(the Charmed Kubeflow jupyter-web-app charm), mounted at
`/etc/config/spawner_ui_config.yaml`.  The charm manages this file through
Juju.  Therefore the right way to update it is via a `juju config` command,
not by editing the file directly (which would be overwritten on the next charm
reconciliation).

---

## Step 6 — Patch `spawner_ui_config` via Juju

### Why Juju and not `kubectl patch`?

Because `jupyter-ui` is a Charmed Kubeflow component managed by Juju.  The
charm controller periodically reconciles the pod configuration.  If you
`kubectl exec` into the pod and manually edit the file, the charm will
overwrite your change the next time it reconciles.  The only persistent way to
change charm-managed configuration is through `juju config`.

### What to run (on the Juju controller VM — `10.110.188.147`):

First, retrieve the current config so you can build the updated version:

```bash
# SSH into the Juju controller VM
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@10.110.188.147

# See current jupyter-web-app config keys
sudo -u ubuntu -i juju config -m kubeflow jupyter-web-app | grep -A5 spawner
```

Then update with the new YAML block (the `|` means a multi-line string in
shell):

```bash
sudo -u ubuntu -i juju config -m kubeflow jupyter-web-app \
  spawner-ui-config="$(kubectl exec -n kubeflow jupyter-ui-0 \
    -c jupyter-ui -- cat /etc/config/spawner_ui_config.yaml \
  | sed 's/    - access-ml-pipeline/    - access-ml-pipeline\n    - elyra-minio-access/')"
```

> **Simpler alternative** if the Juju config key is not available:
> Read the full current YAML, make the two-line edit locally, then apply it
> as a ConfigMap patch:
>
> ```bash
> kubectl exec -n kubeflow jupyter-ui-0 -c jupyter-ui \
>   -- cat /etc/config/spawner_ui_config.yaml > /tmp/spawner.yaml
>
> # Edit /tmp/spawner.yaml — add "    - elyra-minio-access" under access-ml-pipeline
>
> kubectl create configmap jupyter-web-app-config \
>   -n kubeflow \
>   --from-file=spawner_ui_config.yaml=/tmp/spawner.yaml \
>   --dry-run=client -o yaml | kubectl apply -f -
> ```
>
> Then restart the pod to pick up the change:
> ```bash
> kubectl rollout restart statefulset/jupyter-ui -n kubeflow
> ```

### How to verify it worked:

```bash
kubectl exec -n kubeflow jupyter-ui-0 -c jupyter-ui \
  -- grep -A6 configurations /etc/config/spawner_ui_config.yaml
```

Expected output:
```
  configurations:
    readOnly: false
    value:
    - access-ml-pipeline
    - elyra-minio-access
```

---


## Step 7 — Update the image reference from `v1` to `v2`

### What needs to change?

The `spawner_ui_config.yaml` file also controls the list of images users can
pick from in the Notebooks UI.  Currently it does not include
`abidology/elyra` at all (users type it in the "Custom Image" box).

We need to add `abidology/elyra:v2` to the `image.options` list so:
1. Users can select it from a dropdown instead of typing it.
2. Any existing notebooks that were created with `v1` can be updated by the
   user to `v2` — the image tag change is what brings in the new s6 init hook.

### The section to update in `spawner_ui_config.yaml`:

```yaml
image:
  value: abidology/elyra:v2        # ← set as the default image (optional)
  options:
    - charmedkubeflow/jupyter-scipy:1.10.0-0be57a5
    - abidology/elyra:v2           # ← add this line
    - docker.io/charmedkubeflow/jupyter-pytorch-full:v1.10.0
    # ... rest of existing options unchanged
```

### Important note about existing notebooks

Existing notebooks (e.g. the `elyra` notebook in the `la9a` namespace) were
created with `abidology/elyra:v1`.  The image tag is stored in the Notebook
custom resource.  To update them to v2 the user must:

1. Stop the notebook from the Kubeflow UI.
2. Click the edit (pencil) icon.
3. Change the image to `abidology/elyra:v2`.
4. Save and restart.

Alternatively, an admin can patch it directly:

```bash
kubectl patch notebook elyra -n la9a \
  --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"abidology/elyra:v2"}]'
```

---

## Step 8 — Deploy updated `app.py` to the GitLab VM

### What are we deploying and why here?

`app.py` is the Admin Portal backend.  It runs as a `systemd` service on the
GitLab VM (`10.110.188.148`).  It is the code that creates user profiles.  We
added the `apply_elyra_poddefault()` call in Step 4, so now we need to push the
updated file to the server and restart the service.

### Commands (run from your local machine):

```bash
# 1. Copy the updated app.py to the GitLab VM
scp -i ~/.ssh/pfa_bouri_terraform \
  /home/ahmed-bensalah/Micro-K8s\ kubeflow/micro_k8s/ansible/roles/gitlab/files/app.py \
  ubuntu@10.110.188.148:/home/ubuntu/admin-portal/app.py

# 2. Restart the service to load the new code
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@10.110.188.148 \
  "sudo systemctl restart kubeflow-admin-portal"

# 3. Verify the service came back up cleanly
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@10.110.188.148 \
  "sudo systemctl status kubeflow-admin-portal --no-pager | tail -8"
```

### How to confirm the new code is active?

Watch the logs while creating a new test user.  You should see the new log
line:

```
elyra-minio-access PodDefault applied to namespace 'testuser'
```

```bash
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@10.110.188.148 \
  "sudo journalctl -u kubeflow-admin-portal -f"
```

---


## Step 9 — Apply the PodDefault retroactively to existing namespaces

### Why is this step needed?

Steps 4 and 8 ensure that *every future new user* gets the PodDefault
automatically.  But users who were created **before** this system was deployed
(`la9a`, `kfplead`, `lead00`) never went through the updated background task,
so their namespaces do not have `elyra-minio-access` yet.

This step is a one-time catch-up run.

### Verify which namespaces are missing the PodDefault:

```bash
# List all existing profile namespaces
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@10.110.188.144 \
  "kubectl get profiles -o jsonpath='{.items[*].metadata.name}'"
# Expected: la9a kfplead lead00 (and any others)

# Check which ones are missing elyra-minio-access
for ns in la9a kfplead lead00; do
  echo -n "$ns: "
  ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@10.110.188.144 \
    "kubectl get poddefault elyra-minio-access -n $ns 2>&1 | tail -1"
done
```

### Apply it to each missing namespace:

Run this for each namespace that returned "not found":

```bash
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@10.110.188.144 "kubectl apply -f - <<'EOF'
apiVersion: kubeflow.org/v1alpha1
kind: PodDefault
metadata:
  name: elyra-minio-access
  namespace: la9a
spec:
  selector:
    matchLabels:
      elyra-minio-access: \"true\"
  desc: \"Auto-inject MinIO credentials for Elyra KFP runtime\"
  env:
    - name: ELYRA_COS_ENDPOINT
      value: \"http://minio.kubeflow.svc.cluster.local:9000\"
    - name: ELYRA_COS_BUCKET
      value: \"mlpipeline\"
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
EOF"
```

Repeat with `namespace: kfplead` and `namespace: lead00`.

### Verify both PodDefaults now exist side by side:

```bash
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@10.110.188.144 \
  "kubectl get poddefault -n la9a"
```

Expected output:
```
NAME                 AGE
access-ml-pipeline   8d
elyra-minio-access   1m
```

---


## Step 10 — End-to-end test

This is the acceptance test.  All previous steps must be complete before running
this.

### What to check and how

**Check 1 — PodDefault exists in the namespace**

```bash
kubectl get poddefault elyra-minio-access -n la9a -o yaml
```

Look for the `env` section with the four `ELYRA_COS_*` entries.  If missing,
Step 9 did not complete successfully.

---

**Check 2 — Spawn a notebook using `abidology/elyra:v2`**

1. Open the Kubeflow Dashboard at `http://10.110.188.200`
2. Log in as a test user (e.g. `la9a`)
3. Go to **Notebooks → New Notebook**
4. Set the image to `abidology/elyra:v2`
5. In the **Configurations** section, confirm both boxes are ticked:
   - ✅ Allow access to Kubeflow Pipelines
   - ✅ Auto-inject MinIO credentials for Elyra KFP runtime
6. Click **Launch**

---

**Check 3 — Confirm the env vars were injected into the pod**

While the notebook is running:

```bash
kubectl exec -n la9a \
  $(kubectl get pod -n la9a -l notebook-name -o name | head -1) \
  -- env | grep ELYRA
```

Expected output (real values, not placeholders):
```
ELYRA_COS_ENDPOINT=http://minio.kubeflow.svc.cluster.local:9000
ELYRA_COS_BUCKET=mlpipeline
ELYRA_COS_USERNAME=minio
ELYRA_COS_PASSWORD=minio123
```

If you see empty values or nothing, the PodDefault is not matching the pod.
Check that the pod carries the label `elyra-minio-access: "true"`:

```bash
kubectl get pod -n la9a \
  $(kubectl get pod -n la9a -l notebook-name -o name | head -1 | cut -d/ -f2) \
  --show-labels
```

---

**Check 4 — Confirm `default.json` was written by the init script**

```bash
kubectl exec -n la9a \
  $(kubectl get pod -n la9a -l notebook-name -o name | head -1) \
  -- cat /home/jovyan/.local/share/jupyter/metadata/runtimes/default.json
```

Expected output — all fields populated, no `${...}` placeholders, namespace
matches `la9a`:

```json
{
  "schema_name": "kfp",
  "display_name": "default",
  "metadata": {
    "api_endpoint": "http://ml-pipeline.kubeflow.svc.cluster.local:8888",
    "user_namespace": "la9a",
    "engine": "Argo",
    "auth_type": "KUBERNETES_SERVICE_ACCOUNT_TOKEN",
    "cos_endpoint": "http://minio.kubeflow.svc.cluster.local:9000",
    "cos_bucket": "mlpipeline",
    "cos_username": "minio",
    "cos_password": "minio123",
    "cos_auth_type": "USER_CREDENTIALS"
  }
}
```

If the file is missing, the s6 cont-init hook did not run.  Check that the
image being used is actually `v2` and not `v1`.

If the file exists but has empty values, the env vars were not injected — the
PodDefault was not matched (see Check 3).

---

**Check 5 — Verify inside JupyterLab**

1. Open the notebook in JupyterLab (click the **Connect** button in the
   Kubeflow UI)
2. In the left sidebar, click the **Runtimes** icon (looks like a play button
   with a gear)
3. You should see a runtime named **default** already listed
4. Click the edit icon — all fields should be populated with no empty values
5. Close without editing

---

**Check 6 — Run an actual pipeline**

1. In JupyterLab, open the **Pipeline Editor** (File → New → Pipeline)
2. Drag any component node onto the canvas
3. Click **Run Pipeline**
4. Select the `default` runtime configuration
5. Click **OK**

The pipeline should submit successfully to Kubeflow Pipelines.  You can
monitor it in the Kubeflow Dashboard under **Runs**.

---

### Troubleshooting quick-reference

| Symptom | Most likely cause | Where to check |
|---|---|---|
| `default.json` missing | Image is still `v1`, not `v2` | `kubectl describe pod` → Image field |
| `default.json` has empty `cos_username` | PodDefault not matched (label missing) | `kubectl get pod --show-labels` |
| `default.json` has `${ELYRA_COS_USERNAME}` literally | Env var not set at shell time | Check PodDefault applied in namespace |
| Runtime shows in Elyra but pipeline fails | Wrong namespace or token path | Check `user_namespace` in JSON matches pod namespace |
| PodDefault exists but pod has no ELYRA vars | `elyra-minio-access` not in `spawner_ui_config` | Step 6 |
| New users do not get PodDefault | `app.py` not updated / not restarted | Step 8 |

---

## Summary — what each piece does

```
setup-elyra-runtime.sh   writes the JSON config file inside the container
                         reads namespace from K8s service account file
                         reads MinIO creds from environment variables

Dockerfile (v2)          bakes the init script into the image
                         registers it as an s6 cont-init step

elyra-minio-access       a Kubeflow PodDefault — lives in each user namespace
PodDefault               injects 4 ELYRA_COS_* env vars from the MinIO secret
                         only applies to pods labelled elyra-minio-access=true

app.py (updated)         creates the PodDefault when a new user is provisioned
                         runs after the MinIO secret already exists (no race)

spawner_ui_config.yaml   pre-selects elyra-minio-access for all new notebooks
(updated)                so users never have to tick a checkbox
```

Nothing here requires the user to open a terminal, run `kubectl`, or view a
Secret.  The entire chain is automatic from the moment the admin clicks
"Provision" in the Admin Portal.
