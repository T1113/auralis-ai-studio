# Ark Seedream Image Provider

This project standardizes image generation on Volcengine Ark Seedream.

## Defaults

- Base URL: `https://ark.cn-beijing.volces.com/api/v3`
- Endpoint: `/images/generations`
- Default model: `doubao-seedream-5-0-260128`
- Selectable models:
  - `doubao-seedream-5-0-260128`
  - `doubao-seedream-4-5-251128`
  - `doubao-seedream-4-0-250828`
- Size: `2K`
- Response format: `url`
- Watermark: `false`
- Generation mode: image-to-image set
- Sequential generation: `auto`
- Max images per job: `12`

## Request Shape

The Ark image generation endpoint uses one request for a coherent image set:

```json
{
  "model": "doubao-seedream-5-0-260128",
  "prompt": "基于同一张参考自拍一次生成多张职业形象照...",
  "image": "https://public.example.com/static/uploads/photo.jpg",
  "size": "2K",
  "sequential_image_generation": "auto",
  "sequential_image_generation_options": {
    "max_images": 12
  },
  "stream": false,
  "response_format": "url",
  "watermark": false
}
```

For local Flask development, `DOUBAO_PUBLIC_BASE_URL` must point to a public HTTPS tunnel that can reach the current Flask port when `DOUBAO_INPUT_IMAGE_FORMAT=url`. The Ark service downloads `image`, so `curl -I "$DOUBAO_PUBLIC_BASE_URL/static/uploads/<file>"` must return `200` before generation. If Ark returns a URL download timeout, the Flask backend retries the same request with a `data:image/...;base64,...` input to avoid transient ngrok reachability problems.

## Required Secret

Set one of these secrets or environment variables:

```bash
wrangler secret put ARK_API_KEY
# or
wrangler secret put DOUBAO_API_KEY
```

For Flask local development, place the API key in `形象照全栈/.env` as `DOUBAO_API_KEY`.

Optional configuration:

```bash
DOUBAO_MODEL=doubao-seedream-5-0-260128
DOUBAO_IMAGE_SIZE=2K
DOUBAO_RESPONSE_FORMAT=url
DOUBAO_INPUT_IMAGE_FORMAT=url
DOUBAO_MAX_IMAGES=12
```

## Internal Flow

1. Frontend creates a job through `POST /api/jobs`.
2. Worker creates `job_results` rows immediately.
3. If `ARK_API_KEY` or `DOUBAO_API_KEY` is configured, the backend calls Ark Seedream once with `sequential_image_generation: "auto"` and `max_images`.
4. Returned images are written to R2.
5. `job_results` rows are updated to `ready`.
6. Frontend reads `GET /api/jobs/:jobId` and downloads each result through:

```text
GET /api/jobs/:jobId/results/:resultId/download?variant=jpg
```

If no key is configured, the job stays in `reference_only` mode and clearly tells the user it is showing upload references rather than fake AI output.
