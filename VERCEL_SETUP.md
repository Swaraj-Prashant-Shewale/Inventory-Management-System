# Deploy the Inventory Web App to Vercel

The root `app.py` file exposes the existing FastAPI application in the location
Vercel expects. The live application must use the same Supabase database as the
local application; do not deploy the local SQLite database.

## 1. Push the project to GitHub

Commit and push the project, making sure `.env` and `web_secret.key` remain
untracked. They contain secrets and are already excluded by `.gitignore`.

## 2. Import it into Vercel

1. Sign in at https://vercel.com/new.
2. Import the GitHub repository.
3. Leave **Root Directory** at the repository root.
4. Leave **Build Command** and **Output Directory** empty so Vercel detects
   FastAPI from `app.py`.

## 3. Add production environment variables

In **Project Settings > Environment Variables**, add these variables for
Production, Preview, and Development where appropriate:

```text
DB_BACKEND=postgres
DB_NAME=postgres
DB_HOST=<Supabase session-pooler host>
DB_PORT=5432
DB_USER=<restricted inventory_app pooler username>
DB_PASSWORD=<restricted inventory_app password>
DB_SCHEMA=public
DB_SSLMODE=require
DB_SSLROOTCERT=certs/supabase-ca.crt
WEB_SECRET_KEY=<one long random value that never changes>
WEB_COOKIE_SECURE=1
SESSION_IDLE_MINUTES=60
DEBUG=0
```

Use the exact **session pooler** host, port, and username shown in **Supabase > Connect**.
This app sets a tenant schema per database session, so do not use the transaction pooler on port `6543`.

Generate `WEB_SECRET_KEY` locally in PowerShell:

```powershell
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

Store the output only in Vercel and your password manager. Changing it later
signs every user out.

Do not add `ADMIN_DB_USER`, `ADMIN_DB_PASSWORD`, a Supabase service-role key,
or the database owner password to Vercel. Use the limited provisioner setup in
the next section to enable client creation in the hosted admin panel.

## 4. Enable web client provisioning

Run this once from the trusted operator computer:

```powershell
.\.venv\Scripts\python.exe manage_platform.py setup-web-provisioner
```

At the database username prompt, press Enter to accept the displayed
`postgres.<project-ref>` value, then enter the Supabase database password. The
command creates or rotates a non-superuser `inventory_provisioner` role and prints
two values. Add both to Vercel Production as **Sensitive** variables:

```text
PROVISION_DB_USER=<value printed by the command>
PROVISION_DB_PASSWORD=<value printed by the command>
```

This role can create new client schemas and registry rows, but it cannot administer
platform accounts and is not a Supabase database owner.

## 5. Deploy and test

Select **Deploy**. After the build finishes, test these URLs:

```text
https://<project>.vercel.app/admin/login
https://<project>.vercel.app/login
```

The existing platform administrator and company employees will work because
their profiles are stored in Supabase. Use the same usernames and passwords as
locally. Test a check-in and check-out with a non-production item before giving
the URL to the lab.

## Operational note

Vercel runs this application as an automatically scaling function. Login rate
limits in this project are held in each running process, so they are not a
complete distributed brute-force defense. Enable platform-admin 2FA and use
Vercel Firewall rate limiting before exposing the admin login broadly.
