# Game Assets Generation Skill for Meowa

Create game-ready 2D assets with Meowa from inside Codex: pixel sprites, props,
backgrounds, seamless loops, transparent PNGs, simple animations, UI mockups,
and loop-friendly game music drafts.

This repository contains a Codex skill plus a small Python CLI wrapper around
the MeowArt API. The skill teaches an agent how to choose the right generation
path, keep pixel assets crisp, manage output directories, and validate generated
files before handing them back to a game project.

## What It Can Generate

- Pixel characters, enemies, props, items, icons, and sprite batches
- Backgrounds, scene concepts, and 16:9 or other fixed-ratio game art
- Seamless horizontal, vertical, or four-way looping backgrounds and textures
- Transparent PNG assets through pixel or HD background removal
- Pixel-cleaned versions of larger AI-generated images
- Short character or object animations as WebP, GIF, or spritesheets
- Game BGM directions, 30 second demos, or full music generations
- UI concept mockups and extractable UI sprites from game screenshots

## Repository Layout

```text
.
|-- README.md
`-- skills/
    `-- game-assets/
        |-- SKILL.md          # Codex skill instructions
        |-- meowart_api.md    # Quick API usage guide
        `-- meowart_api.py    # MeowArt API helper CLI
```

## Install

Clone the repository and copy the skill into your Codex skills directory:

```bash
git clone https://github.com/MeowjitoAI/meowa-skills.git
cd meowa-skills

export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills/game-assets"
cp -R skills/game-assets/. "$CODEX_HOME/skills/game-assets/"
```

Install the Python dependency used by the helper CLI:

```bash
python3 -m pip install requests
python3 skills/game-assets/meowart_api.py --help
```

## Authentication

Create an API key in Meowa:

https://meowart.ai/#/api-keys

Then set it as an environment variable:

```bash
export MEOWART_API_KEY="ma_live_xxxxxxxxxxxxxxxxxxxx"
```

For internal or self-hosted backend debugging, the CLI also supports developer
auth through `MEOWART_DEV_KEY` or `DEV_API_KEY`, which is sent as `X-Dev-Key`.

Do not commit API keys. Prefer environment variables or a local `.env` file.

## Quick Start

Check available commands:

```bash
python3 skills/game-assets/meowart_api.py --help
```

Check your credit balance:

```bash
python3 skills/game-assets/meowart_api.py credits-balance
```

List pixel generation templates:

```bash
python3 skills/game-assets/meowart_api.py pixel-gen-template-info
```

Dry run a pixel sprite batch without spending credits:

```bash
python3 skills/game-assets/meowart_api.py \
  pixel-gen-run \
  --template-name "cat_2" \
  --requirement "calico, orange tabby, tuxedo, siamese, british shorthair, american shorthair, brown tabby, white cat" \
  --dry-run
```

Generate a side-facing pixel character:

```bash
python3 skills/game-assets/meowart_api.py \
  pixel-gen-run \
  --template-name "pixel_character_large" \
  --requirement "A fox rogue with twin daggers" \
  --template-config '{"direction":"left"}' \
  --output-dir ./outputs/fox_rogue
```

Generate a 16:9 game background:

```bash
python3 skills/game-assets/meowart_api.py \
  gemini-generate-content \
  --text "Generate a 2K 16:9 pixel-art night market background for a cozy RPG." \
  --generation-config '{"responseModalities":["TEXT","IMAGE"],"imageConfig":{"aspectRatio":"16:9","imageSize":"2K"}}' \
  --output-dir ./outputs/night_market
```

Create a seamless horizontal loop from an existing background:

```bash
python3 skills/game-assets/meowart_api.py \
  self-loop-run \
  --image-file ./outputs/night_market/background.png \
  --direction horizontal \
  --output-dir ./outputs/night_market_loop
```

Remove a white background from a pixel sprite:

```bash
python3 skills/game-assets/meowart_api.py \
  remove-background-run \
  --image-file ./outputs/fox_rogue/sprite.png \
  --method pixel \
  --output-dir ./outputs/fox_rogue_transparent
```

Generate a short animation:

```bash
python3 skills/game-assets/meowart_api.py \
  animate-run \
  --image-file ./outputs/fox_rogue_transparent/sprite.png \
  --prompt "fox rogue idle breathing animation" \
  --is-pixel \
  --output-format spritesheet \
  --output-dir ./outputs/fox_rogue_idle
```

Draft loop-friendly game music:

```bash
python3 skills/game-assets/meowart_api.py \
  music-run \
  --prompt "A cozy pixel RPG village theme with flute, kalimba, soft strings, no vocals, loop-friendly"
```

Generate a 30 second music demo:

```bash
python3 skills/game-assets/meowart_api.py \
  music-run \
  --prompt "A cozy pixel RPG village theme with flute, kalimba, soft strings, no vocals, loop-friendly" \
  --audio-generate \
  --demo \
  --output-dir ./outputs/village_theme_demo
```

## Using It With Codex

After the skill is installed, ask Codex for production-oriented game asset work:

```text
Use Meowa to generate eight 64x64 potion icons for a pixel roguelike.
```

```text
Create a side-scrolling forest background, then turn it into a seamless horizontal loop.
```

```text
Generate a transparent idle spritesheet for this character reference.
```

```text
Create a 30 second loop-friendly demo track for a rainy cyberpunk town scene.
```

The skill will guide Codex to:

- Pick the right MeowArt command for the requested asset type
- Use dry runs before expensive generation when useful
- Keep pixel art on stable canvas sizes
- Prefer nearest-neighbor resizing for pixel assets
- Save outputs and metadata into explicit task directories
- Validate dimensions, transparency, frame counts, and downloaded files

## Output Directories

Most `*-run` commands create metadata and output files. For predictable project
integration, pass `--output-dir` for final assets and optionally `--work-dir`
for logs:

```bash
python3 skills/game-assets/meowart_api.py \
  pixel-gen-run \
  --template-name "object" \
  --requirement "eight fantasy key icons, distinct shapes and metals" \
  --work-dir ./.meowart-test/key_icons \
  --output-dir ./assets/generated/key_icons
```

Typical saved files include:

- `meta.json`
- `submit_response.json`
- `job_response.json`
- Downloaded PNG, GIF, WebP, spritesheet, or MP3 files when available

## Production Notes

- Start with one or two assets before generating a full pack.
- Keep one art direction document for style, palette, camera angle, and target sizes.
- For pixel sprites, avoid non-integer scaling and use nearest-neighbor sampling.
- For local edits, keep input and output canvas dimensions stable.
- For batch templates, write the whole batch request, not a single-object prompt.
- For music, start with prompt-only mode, then generate a 30 second demo before a full track.
- Always inspect generated assets before committing them to a game pipeline.

## Documentation

- [Skill instructions](skills/game-assets/SKILL.md)
- [MeowArt API quick guide](skills/game-assets/meowart_api.md)
- [MeowArt API helper CLI](skills/game-assets/meowart_api.py)

## License

No license file is currently included. Add one before redistributing this
repository as an open-source package.
