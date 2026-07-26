# Third-party and model licensing notes

This file is an engineering inventory, not legal advice. A release owner must
verify the exact code, model and media licences used by a deliverable.

## Woosh SFX

`start_woosh.bat` only starts an already provisioned Sony Research Woosh sibling
runtime. It deliberately does not clone code, install packages, or download
weights. Known Hugging Face loaders are forced offline so an incomplete cache
fails closed. The release owner must pin, acquire, hash and review the source,
environment and models separately.

The launcher requires the explicit `--accept-noncommercial` flag because the
open model weights are [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/).
This flag is only a local launcher
acknowledgement; it is not a commercial-clearance control and cannot govern a
Woosh service started by other means. The weights must not be enabled in a
commercial production/release. Required attribution, a licence link and change
indications must accompany permitted uses. A commercial build should either
disable this integration or configure a separately licensed SFX source.

The upstream repository contains mostly MIT code plus Apache-2.0 components.
Downloaded samples carry individual terms. Preserve the applicable upstream
notices and record exact source/model revisions and SHA-256 hashes in the
external release inventory.

The `Woosh/` checkout, virtual environment and multi-gigabyte weights are local
runtime data and are intentionally ignored by the Комбайн Studio repository.

## Stock and references

Pexels, Pixabay, Wikimedia Commons, NASA, Internet Archive, Wikipedia and arXiv
have different terms and attribution requirements. Source URL, author/provider,
licence identifier and required credit are retained in immutable asset variants
and the final Master manifest. Master export blocks stock variants that have no
source URL or licence record. A successful download is still not itself evidence
that an asset is cleared for every commercial use.

## Release checklist

- Select a licence for Комбайн Studio itself and add a root `LICENSE` file.
- Generate the locked Python/npm SBOM with
  `python tools/generate_sbom.py --output artifacts/pipeline-studio.cdx.json`
  and attach it to the release.
- Maintain a separate signed inventory for external binaries and model weights;
  package lockfiles cannot describe FFmpeg, whisper.cpp, ComfyUI or local models.
- Include notices required by code, fonts, models, stock and reference assets.
- Disable all non-commercial integrations in commercial release profiles.
## Script and voice intake

- **yt-dlp** — downloads subtitle tracks and metadata only; video/audio download is disabled by the application.
- **OmniVoice** — optional local TTS runtime invoked through `omnivoice-infer`. On Python 3.13+ its environment also needs `audioop-lts>=0.2.2`, because `audioop` was removed from the Python standard library. Комбайн sends narration as one batch so the model is loaded once per track, not once per fragment.
