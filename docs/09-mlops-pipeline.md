# Walkthrough 09 — ML Application: Employee Attrition Prediction

## Overview

The `mlops-for-devops/` directory contains a complete ML application that
demonstrates the platform's capabilities: a binary classification model
predicting employee attrition, served via REST API and deployed on Kubernetes.

```
mlops-for-devops/
├── phase-1-local-dev/
│   ├── src/                  # Model training code
│   ├── inference/            # FastAPI model serving
│   │   └── src/
│   │       ├── app.py        # API endpoints
│   │       ├── predictor.py  # Model loading + inference
│   │       └── schemas.py    # Pydantic input/output schemas
│   ├── frontend/
│   │   └── app.py            # Flask UI
│   └── k8s/
│       ├── deployment.yaml   # Frontend deployment
│       └── inference.yaml    # KServe InferenceService
│
└── phase-2-enterprise-setup/
    ├── kubeflow-pipeline/    # KFP pipeline definition
    │   ├── pipeline.py
    │   └── config.py
    └── feast/                # Feature store
        ├── feature_definitions.py
        ├── feature_store.yaml
        └── csv_to_parquet.py
```

---

## Phase 1 — Local Development & Serving

### The Model

A binary classification model (`artifacts/model.pkl`) that predicts whether an
employee will leave (1) or stay (0).

**19 input features** (15 direct + 4 derived):

Direct:
- `years_at_company`, `performance_rating`, `no_of_promotions`
- `overtime`, `edu_level`, `no_of_dependents`, `job_level`
- `company_size`, `company_tenure`, `remote_work`
- `company_reputation`, `overall_satisfaction`, `opportunities`
- `annual_income`, `age_group`

Derived (computed in `schemas.py`):
```python
RoleStagnationRatio   = years_at_company / (company_tenure + 1)
TenureGap             = company_tenure - years_at_company
EarlyCompanyTenureRisk = 1 if years_at_company <= 2 else 0
LongTenureLowRoleRisk  = 1 if (company_tenure > 5 and job_level <= 2) else 0
```

These engineered features capture patterns like: an employee who's been in the
company a long time but hasn't been promoted (stagnation risk).

### Inference API (`inference/src/`)

**`predictor.py`:**
```python
class Predictor:
    def __init__(self):
        obj = joblib.load("artifacts/model.pkl")
        self.predict_fn = obj.predict  # bound method

    def predict(self, features: dict) -> dict:
        values = [[features[k] for k in FEATURE_ORDER]]
        probs  = self.predict_fn(values)[0]   # [p_stay, p_leave]
        p_leave = float(probs[1])

        return {
            "prediction": int(p_leave >= THRESHOLD),  # 0=Stay, 1=Leave
            "p_leave": round(p_leave, 4),
            "p_stay":  round(1 - p_leave, 4),
            "risk":    self._tier(p_leave),
            "threshold": THRESHOLD,
        }

    def _tier(self, prob):
        if prob >= 0.65: return "HIGH"
        if prob >= 0.45: return "MEDIUM"
        if prob >= 0.25: return "LOW"
        return "VERY_LOW"
```

**`app.py` (FastAPI):**
```python
@app.post("/predict", response_model=PredictionResponse)
def predict(features: EmployeeFeatures):
    result = predictor.predict(features.to_model_input())
    return result

@app.get("/health")
def health(): return {"status": "ok"}

@app.get("/ready")
def ready(): return {"status": "ready", "model_loaded": predictor.is_loaded()}
```

### Frontend (`frontend/app.py`)

Flask application on port 5000.  Receives form data from the browser,
constructs the payload, and proxies it to the inference service:

```python
MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "http://localhost:8080/predict")

@app.route('/predict', methods=['POST'])
def predict():
    data = request.get_json(force=True)
    payload = { ... 15 fields ... }
    resp = requests.post(MODEL_ENDPOINT, json=payload, timeout=10)
    return jsonify(resp.json())
```

### Kubernetes Deployment (`k8s/`)

**Frontend (`deployment.yaml`):**
```yaml
containers:
  - image: techiescamp/attrition-frontend:1.0.0
    env:
      - name: MODEL_ENDPOINT
        value: "http://employee-attrition-predictor.default.svc.cluster.local:80/predict"
```
NodePort 31010 → port 5000.

**Inference (`inference.yaml`) — KServe:**
```yaml
apiVersion: "serving.kserve.io/v1beta1"
kind: "InferenceService"
metadata:
  name: employee-attrition
spec:
  predictor:
    containers:
      - image: techiescamp/attrition-inference:1.0.0
        ports:
          - containerPort: 8080
        resources:
          requests: { cpu: "200m", memory: "512Mi" }
          limits:   { cpu: "1",    memory: "1Gi" }
```

KServe creates a `Service` named `employee-attrition-predictor` that the
frontend references by DNS.

---

## Phase 2 — Enterprise Setup

### Kubeflow Pipelines (`kubeflow-pipeline/`)

**`config.py`:**
```python
BASE_IMAGE   = "python:3.11-slim"
KFP_ENDPOINT = "https://16.147.48.10:30088"
```

**`pipeline.py`:**
```python
@dsl.pipeline(name="ml-pipeline")
def ml_pipeline(message: str = "hello from pipeline"):
    preprocess_task = preprocess(message=message)
    preprocess_task.set_retry(num_retries=2, backoff_duration="30s")

    train_task = train(input_message=preprocess_task.outputs["Output"])
    train_task.set_retry(num_retries=2, backoff_duration="30s")

    evaluate_task = evaluate(artifact_data=train_task.outputs["artifact_data"])
    evaluate_task.set_retry(num_retries=2, backoff_duration="30s")
```

This pipeline demonstrates three-step parameter and artifact passing in KFP v2:
- `preprocess` → produces an output string
- `train` → receives that string, produces an artifact
- `evaluate` → receives the artifact

Each step has 2 automatic retries with 30s backoff — robust for transient
pod scheduling failures.

Compiled to YAML:
```bash
python pipeline.py
# Outputs: fashion_mnist_pipeline.yaml
```

### Feature Store (Feast)

**`feature_store.yaml`:**
```yaml
project: employee_attrition
provider: local
registry:
  registry_type: sql
  path: postgresql+psycopg://feast:feast@<NODE-IP>:30432/feast
online_store:
  type: redis
  connection_string: <NODE-IP>:30379,password=redispassword,db=0
```

Feast uses:
- **PostgreSQL** (NodePort 30432) as the feature registry and offline store
- **Redis** (NodePort 30379) as the online store for low-latency feature serving

**`feature_definitions.py`** defines 24 features across:
- Demographics (age, gender, marital status, dependents, education)
- Job info (role, level, income, tenure, promotions)
- Work conditions (overtime, remote, distance, work-life balance)
- Company & satisfaction scores
- Target label (attrition 0/1)

A `FeatureService` excludes the target label for inference:
```python
employee_attrition_service = FeatureService(
    name="employee_attrition_service",
    features=[employee_features[["age", "gender", ..., "innovation_opportunities"]]]
)
```

---

## End-to-End Data Flow

```
Data scientist in Kubeflow Notebook
        │
        │  submits pipeline via Elyra or SDK
        ▼
Kubeflow Pipelines API (ml-pipeline.kubeflow.svc:8888)
        │
        ├─ preprocess step (python:3.11-slim container)
        │    reads raw data, outputs cleaned artifact to MinIO
        │
        ├─ train step
        │    loads artifact, trains model, saves model.pkl to MinIO
        │
        └─ evaluate step
             loads model artifact, computes metrics, writes report

Trained model (.pkl) → MinIO → served via KServe InferenceService
                                        │
                                        ▼
                               POST /predict (features)
                                        │
                                        ▼
                               { prediction, p_leave, risk }
                                        │
                                        ▼
                               Frontend UI (port 31010)
```
