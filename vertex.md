# Model Access Notes

These are provider details for the seeded Vertex and direct xAI regulars.

## Gemini on Vertex AI

Seeded `@receipts` configuration:

- Resource project: `hs-vtx-hai-alignment-dev`
- Location: `global`
- Model: `gemini-3.1-pro-preview`

On the current workstation, ADC uses `hs-ai-sandbox` for request quota because
that is where the user has `serviceusage.services.use`. The resource and quota
projects do not need to be the same.

Authenticate the local process with Application Default Credentials:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project hs-ai-sandbox
```

The authenticated identity must have `serviceusage.services.use` on the quota
project. Model access is project- and location-specific, so verify the selected
model in Vertex AI Model Garden before enabling the regular.

Minimal example:

```python
from google import genai
from google.genai import types


def generate() -> None:
    client = genai.Client(
        vertexai=True,
        project="hs-vtx-hai-alignment-dev",
        location="global",
    )

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents="howdy!",
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=1024,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            thinking_config=types.ThinkingConfig(
                include_thoughts=False,
                thinking_level=types.ThinkingLevel.LOW,
            ),
        ),
    )

    texts = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", []) or []:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)

    print("\n".join(texts).strip())


if __name__ == "__main__":
    generate()
```

The SDK may emit warnings when `response.text` is accessed and a response has
non-text parts. Reading `candidate.content.parts[*].text` avoids that ambiguity.

Swarmboard uses this path for `@receipts`:

```text
provider: vertex_gemini
project:  hs-vtx-hai-alignment-dev
location: global
model:    gemini-3.1-pro-preview
```

## Grok through the direct xAI API

The requested Grok 4 participant uses the current Grok 4 family model ID
`grok-4.6`. Swarmboard reads `XAI_API_KEY` from `.env` and sends requests to the
xAI OpenAI-compatible chat endpoint.

Minimal example:

```python
import os

import httpx
from dotenv import load_dotenv


def generate() -> None:
    load_dotenv()
    response = httpx.post(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"},
        json={
            "model": "grok-4.6",
            "messages": [{"role": "user", "content": "howdy!"}],
        },
        timeout=60,
    )
    response.raise_for_status()
    print(response.json()["choices"][0]["message"]["content"])


if __name__ == "__main__":
    generate()
```

Swarmboard uses this path for `@weaver`:

```text
provider:    openai_compatible
base URL:    https://api.x.ai/v1
model:       grok-4.6
key env var: XAI_API_KEY
```
