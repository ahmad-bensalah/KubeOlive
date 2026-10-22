#!/usr/bin/env python3
"""
patch_elyra.py — applied at Docker build time.

When PIPELINE_SCOPE=private (the default), patches kfp_processor.py to pass
namespace=user_namespace to client.upload_pipeline(), making pipelines
private (namespace-scoped) instead of shared across all KFP users.

When PIPELINE_SCOPE=shared, does nothing — keeps original Elyra behavior.
"""
import os
import sys
import inspect

scope = os.environ.get("PIPELINE_SCOPE", "private")

if scope != "private":
    print(f"PIPELINE_SCOPE={scope} — skipping patch, pipelines will be shared (visible to all users)")
    sys.exit(0)

import elyra.pipeline.kfp.kfp_processor as m
path = inspect.getfile(m)

with open(path, "r") as f:
    src = f.read()

OLD = (
    "                    kfp_pipeline = client.upload_pipeline(\n"
    "                        pipeline_package_path=pipeline_path,\n"
    "                        pipeline_name=pipeline_name,\n"
    "                        description=pipeline_description,\n"
    "                    )"
)
NEW = (
    "                    kfp_pipeline = client.upload_pipeline(\n"
    "                        pipeline_package_path=pipeline_path,\n"
    "                        pipeline_name=pipeline_name,\n"
    "                        description=pipeline_description,\n"
    "                        namespace=user_namespace,\n"
    "                    )"
)

if OLD not in src:
    print(f"ERROR: patch target not found in {path}")
    print("The indentation in kfp_processor.py may have changed in this Elyra version.")
    sys.exit(1)

patched = src.replace(OLD, NEW, 1)

if "namespace=user_namespace" not in patched:
    print("ERROR: replacement failed")
    sys.exit(1)

with open(path, "w") as f:
    f.write(patched)

print(f"SUCCESS: patched {path}")
print("Pipelines submitted by this image will be private (namespace-scoped)")
