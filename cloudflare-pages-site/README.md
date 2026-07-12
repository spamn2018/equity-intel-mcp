# Cloudflare Pages Frontend

This folder is a standalone static frontend for the trading performance dashboard.

It is designed for a free Cloudflare Pages deployment and reads live data from an existing backend API that exposes:

- `GET /api/trading/workflow`

## What it does

- Hosts the UI as a static site on `*.pages.dev`
- Fetches fresh workflow data on page load and on manual refresh
- Lets you configure the backend API base URL in the browser and saves it to `localStorage`
- Keeps all broker credentials and execution logic on the backend, not in the frontend

## Deploy to Cloudflare Pages

1. Create a new Pages project.
2. Upload this folder as a static HTML site.
3. After the site is live, open it and click `API Settings`.
4. Enter the public base URL of your backend, for example `https://your-api.example.com`.
5. Save and refresh.

You can also prefill the URL by editing `config.js` before deploy.

## Notes


- The backend must be reachable from the public internet.
- The backend must allow CORS for the Pages domain. Your current Flask app already sends `Access-Control-Allow-Origin: *`.
- Do not expose live trade-execution routes publicly just because the frontend is public.

## Cloudflare docs

- Static HTML: https://developers.cloudflare.com/pages/framework-guides/deploy-anything/
- Direct Upload: https://developers.cloudflare.com/pages/get-started/direct-upload/


## Automatic updates

To make the site redeploy automatically, connect this repository to Cloudflare Pages Git integration or add a GitHub Actions deploy workflow using a token that includes the `workflow` scope.
