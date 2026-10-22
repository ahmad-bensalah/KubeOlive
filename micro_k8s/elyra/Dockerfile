FROM ghcr.io/kubeflow/kubeflow/notebook-servers/jupyter:v1.8.0

# ── Build arg controls pipeline visibility ────────────────────────────────────
# PIPELINE_SCOPE=private  → pipelines scoped to user namespace (default, v3)
# PIPELINE_SCOPE=shared   → pipelines visible to all users (v3-shared)
ARG PIPELINE_SCOPE=private

USER root

RUN python3 -m pip install --upgrade pip

RUN python3 -m pip install \
    "elyra[kfp,kfp-examples,airflow-gitlab]"

# ── Fix: control pipeline visibility via PIPELINE_SCOPE build arg ─────────────
COPY patch_elyra.py /tmp/patch_elyra.py
RUN PIPELINE_SCOPE=${PIPELINE_SCOPE} python3 /tmp/patch_elyra.py && \
    rm /tmp/patch_elyra.py

# ── entrypoint wrapper ────────────────────────────────────────────────────────
COPY setup-elyra-runtime.sh /usr/local/bin/setup-elyra-runtime.sh
RUN chmod +x /usr/local/bin/setup-elyra-runtime.sh
# ──────────────────────────────────────────────────────────────────────────────

# Switch to jovyan BEFORE generating config and creating catalogs so all
# metadata files are owned by jovyan and written to /home/jovyan/.jupyter/
# Running these as root writes to /root/.jupyter/ which JupyterLab never reads.
USER jovyan

RUN jupyter elyra --generate-config && \
    sed -i \
    "s/# c.PipelineProcessorRegistry.runtimes = \[\]/c.PipelineProcessorRegistry.runtimes = \['kfp'\]/g" \
    $(jupyter --config-dir)/jupyter_elyra_config.py

RUN elyra-metadata create component-catalogs \
    --schema_name=elyra-kfp-examples-catalog \
    --display_name="Kubeflow Pipelines examples" \
    --runtime_type=KUBEFLOW_PIPELINES \
    --categories="['examples']"

ENTRYPOINT ["/usr/local/bin/setup-elyra-runtime.sh"]
