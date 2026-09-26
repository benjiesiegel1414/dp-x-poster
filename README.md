# DividendProjection X poster

Internal tool. `tool.html` builds a daily queue of posts with images from the live yield sheet. GitHub Actions posts them to X on schedule.

## One-time setup

### 1. X developer app
1. Go to console.x.com and sign in as the account you post from.
2. Create an app. In its user authentication settings, set app permissions to **Read and write**.
3. Add credits (pay-per-use). About $5/month covers 10 image posts a day without links.
4. Under Keys and tokens, copy the **API Key and Secret**, then generate the **Access Token and Secret**. Generate the access token *after* setting Read and write, or it will be read-only.

### 2. GitHub repo
1. Create a new repo named `dp-x-poster` and upload every file in this folder, keeping the folders (`.github/workflows`, `scripts`, `queue`, `posted`, `failed`).
2. Settings > Secrets and variables > Actions > New repository secret. Add four:
   `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`
3. Actions tab > Post queued tweets > Run workflow > tick "Only test the X connection". The log should say `Connected as @DevotedDividend`. Nothing is posted.

### 3. Token for the tool
1. GitHub > Settings > Developer settings > Fine-grained tokens > Generate new token.
2. Repository access: only `dp-x-poster`.
3. Permissions: **Contents** read and write, **Actions** read and write.
4. Open `tool.html` on your computer (double-click it), click Settings, paste the token.

## Daily use
1. Open `tool.html`, pick the day, click **Generate queue**.
2. Swap funds, change numbers, edit text, remove any you don't like.
3. Click **Schedule all**. Posts go out within about 15 minutes of their time (GitHub can run a bit late at busy times).

## Good to know
- Posts without links cost $0.015 each on X's API. Posts with a link cost $0.20.
- The tool never repeats a fund + amount combo that's already queued or posted.
- If Actions is down and a post is more than 6 hours late, it's skipped and shows under Failed instead of posting late.
- A post that errors 3 times moves to Failed with the error message.
- The workflow only runs during posting hours (about 8am to 10:45pm Eastern) to save Actions minutes.
