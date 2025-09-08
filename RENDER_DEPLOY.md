# Render Deployment Instructions

## Prerequisites
1. Render account
2. GitHub repository with this code

## Important Notes
- Times are configured for UAE timezone (UTC+4)
- Morning digest: 9am UAE (5am UTC)
- Afternoon digest: 2pm UAE (10am UTC)

## First-Time Setup

### 1. Prepare the Code
```bash
# Remove sensitive files before pushing to GitHub
rm -f .env .env.* msal_token_cache*.bin token.pkl creds.json
```

### 2. Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit for email digest"
git remote add origin YOUR_GITHUB_REPO_URL
git push -u origin main
```

### 3. Initial Authentication (IMPORTANT!)

Before deploying to Render, you need to generate Jawad's authentication token locally:

```bash
# Run locally to authenticate Jawad
USER_PROFILE=jawad python main.py

# This will show a device code - have Jawad:
# 1. Visit the URL shown
# 2. Enter the code
# 3. Sign in with jawad@multiply.ae

# This creates msal_token_cache_jawad.bin
```

### 4. Deploy to Render

1. Go to [Render Dashboard](https://dashboard.render.com)
2. Click "New +" → "Blueprint"
3. Connect your GitHub repo
4. Render will detect `render.yaml` automatically

### 5. Set Secret Environment Variables

In Render Dashboard, add these environment variables:
- `OPENAI_API_KEY` = [Jawad's OpenAI API key]
- `AZURE_CLIENT_ID` = 553c30ed-4346-461b-ac97-31d9b5e4daa4
- `AZURE_TENANT_ID` = common

### 6. Upload Token Cache as Secret File

**Using Render Secret Files (Recommended)**

1. In your Render service dashboard, go to "Settings" → "Secret Files"
2. Click "Add Secret File"
3. Set path: `/etc/secrets/msal_token_cache_jawad.bin`
4. Upload the `msal_token_cache_jawad.bin` file
5. The app will automatically find it at runtime

**Alternative: Environment Variable**
If secret files don't work, use:
```bash
python prepare_render_token.py jawad
```
Then add the `MSAL_TOKEN_CACHE_BASE64` environment variable from the output.

## Schedule

The cron jobs will run automatically in UAE time:
- **9:00 AM UAE (5:00 AM UTC)**: Morning digest (2pm yesterday → 9am today)
- **2:00 PM UAE (10:00 AM UTC)**: Afternoon digest (9am → 2pm today)

## Monitoring

Check Render dashboard for:
- Job execution logs
- Success/failure status
- Email delivery confirmation

## Troubleshooting

### Token Expired
1. Re-authenticate locally
2. Re-upload the token cache file

### No Emails Sent
1. Check logs in Render dashboard
2. Verify OpenAI API key is set
3. Ensure Jawad has emails in the time range

### Time Zone Issues
Render uses UTC. Adjust the cron schedule in `render.yaml` if needed:
- Dubai time = UTC+4
- So 9am Dubai = 5am UTC
- And 2pm Dubai = 10am UTC