#  06 — Elyra Notebook Image & Auto-Configuration

## What is Elyra?

Elyra is a JupyterLab extension that adds a visual pipeline editor to the
notebook environment. Instead of writing Kubeflow Pipeline YAML by hand, a
data scientist drags components onto a canvas, connects them, and clicks
"Run Pipeline". Elyra handles the submission to the Kubeflow Pipelines API
and the upload of artifacts to MinIO.

Before Elyra can submit a pipeline, it needs a **runtime configuration** — a
JSON file that tells it:
- Where the KFP API server is
- Which MinIO bucket to use
- What credentials to authenticate with MinIO

On a standard installation, every user has to find and enter these values
manually. On this platform, that configuration is written automatically the
moment a notebook starts. The user opens JupyterLab and the runtime is already
there, fully connected.

---

## The Three Components That Work Together

### 1. The Custom Notebook Image

The platform uses a custom Docker image (`ahmed8603/elyra:v2`) built on top of
the official Charmed Kubeflow notebook base. It has two additions beyond the
standard image:

The first addition is the Elyra extension itself, installed with full KFP
support. This gives JupyterLab the visual pipeline editor, the runtime
configuration panel, and the KFP component catalog.

The second addition is a small shell script that runs inside the container
before JupyterLab opens. This script's job is to write the Elyra runtime
configuration file to the right location on disk — but only if the file does
not already exist, so that any manual changes a user makes are never overwritten
on container restart.

The script reads four environment variables (`ELYRA_COS_ENDPOINT`,
`ELYRA_COS_BUCKET`, `ELYRA_COS_USERNAME`, `ELYRA_COS_PASSWORD`) to fill in the
MinIO connection details, and reads the pod's namespace from a file that
Kubernetes automatically mounts inside every container. This means the
namespace is never hardcoded — the same image works for every user.

The script runs as an **s6-overlay init hook**. s6-overlay is the process
supervisor baked into the Charmed Kubeflow notebook base. It runs a chain of
one-shot scripts before starting JupyterLab. Our script is registered as one
of those steps, so by the time the user connects to JupyterLab, the runtime
file is already written.

### 2. The PodDefault (per user namespace)

A PodDefault is a Kubeflow custom resource that acts like a template patch
applied to pods. When a notebook pod starts in a namespace that has a
PodDefault, and the pod carries the matching label, the Kubeflow admission
webhook automatically injects whatever the PodDefault specifies into the pod
before its containers start.

For Elyra, there is a PodDefault named `elyra-minio-access` in each user's
namespace. It injects the four `ELYRA_COS_*` environment variables, sourcing
the actual credentials from the `mlpipeline-minio-artifact` secret that
already exists in the namespace. This means the MinIO password is never
stored in plaintext in the PodDefault itself — it references the secret by
key name.

The timing matters here. The PodDefault references the MinIO secret, so the
secret must already exist in the namespace before the PodDefault can be
applied. The Admin Portal handles this correctly: it first waits for the
namespace to be ready, then replicates the MinIO secret into it, and only
then applies the PodDefault. This sequence is enforced by the background task
in `kubernetes.py`.

---

## The Full Sequence When a User Starts a Notebook

1. The admin provisions a user in the Admin Portal. The background task waits
   for the namespace, copies the MinIO secret, applies the Elyra PodDefault,
   and marks the user as READY.

2. The user opens the Kubeflow dashboard, navigates to Notebooks, and clicks
   New Notebook. They select `ahmed8603/elyra:v2` from the image dropdown.
   The "Auto-inject MinIO credentials for Elyra KFP runtime" configuration is
   already pre-selected.

3. They click Launch. Kubernetes creates a StatefulSet and a pod. Before the
   pod's containers start, the Kubeflow admission webhook sees the
   `elyra-minio-access: "true"` label, finds the matching PodDefault in the
   namespace, and patches the pod spec to add the four environment variables.

4. The container starts. The s6-overlay init system runs the setup script as
   a one-shot step. The script checks that `default.json` does not exist,
   reads the namespace and the four environment variables, and writes the
   fully-populated runtime configuration file to disk.

5. s6-overlay hands off to JupyterLab, which starts normally.

6. The user opens JupyterLab. They click the Runtimes panel in the left
   sidebar and see a runtime named "default" already listed, with all fields
   filled in. They open the Pipeline Editor and can immediately submit a
   pipeline without any additional configuration.


---

## Verifying It Works

After provisioning a user and starting a notebook with the Elyra image, three
things can be confirmed from the command line:

First, confirm the PodDefault exists in the user's namespace and references
the correct secret. Second, confirm the environment variables are present
inside the running pod. Third, confirm the `default.json` file was written
and contains no placeholder values — all fields should show actual endpoints
and credentials, and the `user_namespace` field should match the user's
namespace name.

From inside JupyterLab, the final confirmation is opening the Runtimes panel
and seeing the "default" runtime listed with all fields populated, then
successfully submitting a pipeline run from the Pipeline Editor.

---

## What Breaks If a Step Is Skipped

| Skipped step | Symptom |
|---|---|
| PodDefault not applied | `default.json` has empty credential fields; pipelines fail with MinIO auth errors |
| MinIO secret not replicated first | PodDefault can't reference the secret; pod stays in `ContainerCreating` |
| `elyra-minio-access` not in default-poddefaults | User has to manually tick the configuration checkbox on every notebook |
| Using the `v1` image instead of `v2` | Init script is not present; `default.json` is never written |
| Writing `default.json` without the guard | User config is overwritten on every container restart |
