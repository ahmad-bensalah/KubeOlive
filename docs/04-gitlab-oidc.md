#  04 — GitLab CE as OIDC Identity Provider

## Overview

GitLab serves two purposes in this platform:
1. **Source of truth for user accounts** — the Admin Portal creates GitLab
   accounts for every MLOps engineer
2. **OIDC identity provider** — Kubeflow's Dex authenticator uses "Login with
   GitLab" so users authenticate with their GitLab credentials

**Role:** `roles/gitlab/tasks/main.yml`  
**Target:** `gitlab_machine` (10.110.188.148)

---

## Architecture

```
User browser
     │
     │  http://10.110.188.200  (Kubeflow dashboard)
     ▼
Istio Ingress Gateway
     │
     ▼
Dex (OIDC server — kubeflow namespace)
     │
     │  OAuth2 Authorization Code Flow
     ▼
GitLab CE  (http://10.110.188.148)
     │
     │  issues JWT token with user identity
     ▼
Dex validates token, creates Kubeflow session
```

---

## Step 1 — Install GitLab CE

```yaml
- name: Install prerequisites
  apt: { name: [curl, openssh-server, postfix, ca-certificates] }

- name: Configure postfix (non-interactive)
  debconf: { name: postfix, question: postfix/main_mailer_type, value: "Internet Site" }

- name: Add GitLab apt repo
  shell: curl -sS https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh | bash

- name: Install GitLab CE
  apt: { name: gitlab-ce }
  environment:
    EXTERNAL_URL: "http://{{ ansible_host }}"   # http://10.110.188.148
```

The `EXTERNAL_URL` environment variable is read by GitLab's installer to set
the Nginx `server_name` and all internal URLs.  Everything — OAuth callbacks,
email links, git clone URLs — uses this value.

---

## Step 2 — Fix CSRF Race Condition

With Puma's default multi-worker mode, GitLab's login form fails randomly because:
- Worker A generates a CSRF token and stores it in the session
- Nginx round-robins the form submission to Worker B
- Worker B does not have that session → CSRF check fails → login error

```ruby
# /etc/gitlab/gitlab.rb
puma['worker_processes'] = 0   # single-worker mode
puma['min_threads'] = 1
puma['max_threads'] = 8

gitlab_rails['trusted_proxies'] = [
  '10.110.188.0/24',   # LAN
  '172.16.0.0/16',     # Kubernetes pod CIDR
  '172.17.0.0/18'      # Kubernetes service CIDR
]
```

The trusted_proxies setting tells Rails that `X-Forwarded-For` headers from
Kubernetes Istio and MetalLB ingress are trustworthy, preventing CSRF failures
on OAuth callbacks that go through the Kubeflow reverse proxy chain.

```bash
gitlab-ctl reconfigure    # applies all gitlab.rb changes
```

---

## Step 3 — Register the Kubeflow OAuth Application

Instead of using the GitLab web UI, we use `gitlab-rails runner` to execute
Ruby code directly in GitLab's Rails console:

```ruby
# Creates a Doorkeeper (OAuth2) application named "Kubeflow Dex"
app = Doorkeeper::Application.find_or_create_by(name: 'Kubeflow Dex') do |a|
  a.redirect_uri  = 'http://10.110.188.200/dex/callback'
  a.scopes        = 'openid profile email read_user'
  a.confidential  = true
end

# Write credentials to a temp file so Ansible can fetch them
File.write('/tmp/gitlab_oauth_creds.json',
  { client_id: app.uid, client_secret: app.secret }.to_json
)
```

**OAuth flow explained:**
1. User clicks "Login with GitLab" on the Kubeflow page
2. Browser redirects to `http://10.110.188.148/oauth/authorize?client_id=...`
3. User logs in to GitLab and approves the scope request
4. GitLab redirects to `http://10.110.188.200/dex/callback?code=...`
5. Dex exchanges the code for a token at GitLab's token endpoint
6. GitLab returns the user's `openid profile email` claims
7. Dex issues a Kubeflow session JWT

**Scopes:**
- `openid` — required for OIDC (issues ID token with sub/iss/aud)
- `profile` — user's name and username
- `email` — user's email address (used as Kubeflow identity)
- `read_user` — access to GitLab's `/api/v4/user` endpoint for additional claims

---

## Step 4 — Fetch Credentials to Ansible Controller

```yaml
- name: Fetch OAuth credentials
  ansible.builtin.fetch:
    src: /tmp/gitlab_oauth_creds.json
    dest: "{{ playbook_dir }}/configs/gitlab_oauth_creds.json"
    flat: true
```

The Juju play reads this file to configure Dex:
```yaml
gitlab_oauth_creds: "{{ lookup('file', playbook_dir + '/configs/gitlab_oauth_creds.json') | from_json }}"
```

---

## Step 5 — Deploy Admin Portal

See **Walkthrough 07** for the full portal walkthrough.  The `portal.yml` task
file is included here:

```yaml
- name: Include Portal provisioning tasks
  ansible.builtin.include_tasks: portal.yml
  tags: [portal]
```

---

## Key Design Decisions

| Decision | Reason |
|---|---|
| Plain IP, not nip.io domain | nip.io is in the Public Suffix List — Chrome/Safari block cookies, breaking Dex CSRF state |
| `gitlab-rails runner` vs REST API | Does not require a pre-existing admin token; runs with full DB access |
| Single Puma worker | Eliminates cross-worker CSRF token failures; acceptable for internal deployment |
| Trusted proxies for K8s CIDRs | Without this, Rails rejects OAuth callbacks from Istio as coming from "unknown" proxies |

---

## Verifying GitLab OIDC

```bash
# Test the OAuth app exists
curl -s http://10.110.188.148/api/v4/applications \
  -H "Private-Token: <admin-token>" | python3 -m json.tool

# Manually trigger the OIDC flow (returns auth page HTML)
curl -s "http://10.110.188.148/oauth/authorize?\
client_id=<client_id>&\
redirect_uri=http://10.110.188.200/dex/callback&\
response_type=code&\
scope=openid+profile+email" -I

# Check Dex can reach GitLab from inside the cluster
kubectl exec -n kubeflow dex-0 -- \
  wget -qO- http://10.110.188.148/.well-known/openid-configuration
```
