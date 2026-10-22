#!/bin/bash

set -e

echo "======================================"
echo " Deploy Charmed MLflow"
echo "======================================"

# Deploy MLflow
juju deploy mlflow-server

# Deploy MariaDB for MLflow metadata
juju deploy charmed-osm-mariadb-k8s mlflow-db


echo "Waiting for applications..."
sleep 60


echo "======================================"
echo " Integrate MLflow Database"
echo "======================================"

juju integrate \
  mlflow-server:relational-db \
  mlflow-db:mysql


echo "======================================"
echo " Integrate MinIO Object Storage"
echo "======================================"

juju integrate \
  mlflow-server:object-storage \
  minio:object-storage


echo "======================================"
echo " Integrate Istio Ingress"
echo "======================================"

juju integrate \
  mlflow-server:ingress \
  istio-pilot:ingress


echo "======================================"
echo " Add MLflow to Kubeflow Dashboard"
echo "======================================"

juju integrate \
  mlflow-server:dashboard-links \
  kubeflow-dashboard:links


echo "======================================"
echo " Deploy Resource Dispatcher"
echo "======================================"

# Needed for newer Charmed Kubeflow versions
juju deploy resource-dispatcher \
  --channel 2.0/stable \
  --trust || true


echo "======================================"
echo " Integrate Secrets and Pod Defaults"
echo "======================================"

juju integrate \
  mlflow-server:secrets \
  resource-dispatcher:secrets || true


juju integrate \
  mlflow-server:pod-defaults \
  resource-dispatcher:pod-defaults || true


echo "======================================"
echo " Final Juju Status"
echo "======================================"

juju status --relations


echo "======================================"
echo " Kubernetes Status"
echo "======================================"

kubectl get pods -n kubeflow | grep -E "mlflow|resource"


echo "======================================"
echo " DONE"
echo " Refresh Kubeflow Dashboard"
echo "======================================"