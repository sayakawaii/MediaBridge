# Getting your AcFun cookies

MediaBridge authenticates with a browser session cookie. There is no API token, and password login is deliberately not implemented: AcFun serves a captcha once the same account logs in repeatedly, which a scheduled job does by definition. A cookie exported once a month is both simpler and less likely to trip fraud detection.

## What you need

Two cookies actually matter:

| Cookie | What it is |
| --- | --- |
| `acPasstoken` | The session token |
| `auth_key` | Your numeric user ID |

Anything else in the export is optional. MediaBridge discards analytics cookies (`Hm_lvt_*`, `_ga`, …) automatically rather than storing your browsing history in a secret.

## Option A: Cookie-Editor (easiest)

1. Install the [Cookie-Editor](https://cookie-editor.com/) extension.
2. Log in at <https://www.acfun.cn>.
3. Open Cookie-Editor on any acfun.cn page and choose **Export → Export as JSON**.
4. Paste the result into the `ACFUN_COOKIE` secret.

## Option B: DevTools

1. Log in at <https://www.acfun.cn>.
2. Open DevTools → **Application** → **Cookies** → `https://www.acfun.cn`.
3. Build a single line from the two required cookies:

```
acPasstoken=<value>; auth_key=<value>
```

Both the JSON array and this one-line header form are accepted, as are raw `Set-Cookie:` response headers pasted verbatim — MediaBridge detects the format.

## Storing it

**As a GitHub secret** (for the workflow): repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**, named `ACFUN_COOKIE`.

**Locally** (for testing), put it somewhere outside the repository and lock down the permissions:

```bash
mkdir -p ~/.mediabridge && chmod 700 ~/.mediabridge
# paste the cookie blob into the file
chmod 600 ~/.mediabridge/ac_cookie.txt
```

MediaBridge reads `$ACFUN_COOKIE` first and falls back to `~/.mediabridge/ac_cookie.txt`. The repository's `.gitignore` excludes `*cookie*`, but do not rely on that — keep credentials out of the working tree entirely.

## Checking it

```bash
mediabridge login-check
```

```
Credentials loaded from /home/you/.mediabridge/ac_cookie.txt
Cookies present: _did, acPasstoken, ac_username, auth_key
AcFun cookie valid for another 30 days.
Session is valid (uid=100912, membership=1, signed-in-today=True)
```

## When it expires

AcFun sessions last roughly a month. MediaBridge warns when fewer than seven days remain, and the workflow's first step is `login-check`, so an expired session fails the job in seconds rather than after downloading gigabytes that could never have been uploaded.

To renew, repeat the export and update the secret. Note that the expiry date attached to the cookie is only a hint — AcFun can invalidate a session earlier, for instance if you log out in the browser or sign in from elsewhere. `login-check` is the authority.

## If something goes wrong

| Symptom | Cause |
| --- | --- |
| `missing required cookie(s): acPasstoken` | The export was taken while logged out, or from the wrong domain. Confirm you are logged in and reading cookies for `acfun.cn`. |
| `AcFun rejected the stored cookies` | The session has been invalidated. Export a fresh one. |
| `AcFun redirected ... to login` | Same, but detected mid-run. Member API endpoints redirect rather than returning 401. |
