# Security

What is in place, what is deliberately not, and what still needs doing.

No system is unbreakable. The aim here is that a compromise needs real effort, is
contained when it happens, and leaves evidence.

---

## Database access

The application runs as **`inventory_app`**, a least-privilege role created by
`security_setup.py`. Re-run that script to rotate its password or rebuild its grants.

| The application role can | The application role cannot |
|---|---|
| Read and write business data | Create, alter or drop any table |
| Use the id sequences | Create roles or databases |
| Null a user reference on an audit row (needed when deleting a user) | Delete or rewrite audit entries |
| Delete a stock movement when its item is deleted | Change a movement's quantity, balance, cost or date |
| Work inside `public` | Touch any other schema |

Verified from the role's own session, not assumed — `security_setup.py` reconnects as
`inventory_app` and proves each restriction before printing the credentials.

**The `postgres` superuser password must never be deployed to a warehouse PC.** It is
needed only for schema migrations and for re-running `security_setup.py`. Keep it in a
password manager, not in any `.env` that ships.

### Why the ledgers are frozen

`audit_log` and `stock_movement` are append-only by grant. Someone who steals the
application credentials can still cause damage, but cannot quietly rewrite stock history
or erase the record of what they did. That turns an invisible compromise into a visible
one.

## Row Level Security

RLS is enabled on all 41 tables by the project's automatic-RLS trigger. `inventory_app`
holds `BYPASSRLS`, so RLS is not what protects the app's own data — the grants above are.
RLS is there to stop the PostgREST roles (`anon`, `authenticated`) reading anything if
the Data API is ever switched on.

## Authentication

- **PBKDF2-HMAC-SHA256, 600,000 iterations**, per-password random salt. Stored hashes
  record the work factor they were made with, so raising it is backward compatible and
  old hashes upgrade automatically at next sign-in.
- **Account lockout** after 5 failed attempts, for 10 minutes. The correct password is
  refused while locked.
- **Every failed sign-in is audited** with the username — never the attempted password.
- **A reset password must be changed.** An Owner resetting someone's password issues a
  temporary one; the next sign-in forces a change and refusing it blocks entry.
- **Idle lock** after `SESSION_IDLE_MINUTES` (default 20). Re-enter the password to
  resume; failing ends the session. Warehouse terminals are shared and get abandoned.

## Transport

**TLS 1.3, AES-256-GCM, with `sslmode=verify-full`** — the connection is encrypted *and*
the server's identity is verified, so an active machine-in-the-middle is not possible.

Supabase's certificates do not chain to a public root, so a copy of their CA lives in
`certs/supabase-ca.crt`. The pooler sends its whole chain during the handshake, so that
file was built from the live connection rather than downloaded. It contains:

| | Subject | Expires |
|---|---|---|
| Root | `CN=Supabase Root 2021 CA` | 26 Apr 2031 |
| Intermediate | `CN=Supabase Intermediate 2021 CA` | 21 Oct 2033 |

SHA-256 fingerprints, so you can confirm they are genuine:

```
Root         807025ad50d4ed219d2c9c7d299c004f824eb00cf7f65afef607d07b72e6cafa
Intermediate 303b0a59bbc8d77e967fbed20b3fe68ec5d7d391c3081ece9936efceef0a55ea
```

**Worth doing once:** download the certificate from the Supabase dashboard
(Project Settings → Database → SSL configuration) and check it matches. Extracting a CA
from the connection you are about to trust is trust-on-first-use — sound in practice, but
an independent copy removes even that assumption. Replacing the file needs no code
change.

`config.py` upgrades `sslmode` to `verify-full` automatically whenever the CA file is
present, and logs a warning on every start when it is not. The certificate is copied
beside the executable by `build_exe.py`, so it can be swapped without a rebuild.

## Application

- All SQL is parameterised through peewee. The one piece of interpolated SQL — setting
  `search_path` — is guarded by a strict identifier pattern.
- Roles: Owner / Manager / Clerk, with a 20-capability matrix, **enforced in the service
  layer** — every operation that writes business data calls `auth.require()` before doing
  anything, so authorisation never depends on a button having been disabled. Operations
  with no signed-in user are refused outright rather than treated as trusted; an
  unattributed write is exactly what should not be possible.
- Deletion is guarded: items holding stock cannot be deleted, nor can warehouses,
  suppliers or customers with history.
- Unhandled exceptions are logged and shown without leaking credentials — verified that
  the database password appears in neither the message nor the traceback.

## Known limitations — read these

**Role permissions are enforced by the application, not the database.** Anyone who can
read a warehouse PC's `.env` can connect with a SQL client and do anything the
`inventory_app` role permits, regardless of whether they are a Clerk in the app. Service
layer checks stop a modified or misused client; they cannot stop someone bypassing the
client entirely. The database grants are the boundary that holds in that case — which is
why the ledgers are frozen there rather than only in code.

Closing this properly would mean a database role per application role, so Postgres itself
refuses a clerk's payment. That is a sizeable change and probably overkill here; the
combination of restricted grants, frozen ledgers and network restrictions gets most of
the benefit.

**Credentials sit in plaintext on each PC.** `.env` is readable by anyone who can read
the file. Restrict the folder with NTFS permissions to the accounts that need it, and
prefer per-site credentials so one leaked file can be revoked without touching every
site.

**No network restrictions yet.** This is the highest-value control still available and
costs nothing: Supabase dashboard → Project Settings → Database → Network Restrictions,
allowlisting each warehouse's public IP. A leaked password then becomes unusable from
anywhere else. It needs the IPs, which is why it is not done.

**No backups configured.** Ransomware and a careless `DELETE` look the same afterwards.
Supabase's automatic backups depend on your plan — check what retention you actually
have before going live.

## If you suspect a compromise

1. Rotate the application role: re-run `security_setup.py`, then update every PC's `.env`.
2. Rotate the `postgres` password from the Supabase dashboard.
3. Read `audit_log` — it cannot have been altered by the application role. Look for
   `LOGIN_FAILED` bursts, unexpected `LOGIN` times, `DELETE` actions, and
   `PASSWORD_RESET`.
4. Reconcile `stock_movement` against `stock_level`; the movement history cannot have
   been rewritten, so the ledger is the authority.
