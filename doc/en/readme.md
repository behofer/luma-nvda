# Luma - AI Companion for NVDA

**Luma** is an NVDA add-on that brings AI-powered visual description, screen analysis, and conversational chat to blind users. It supports multiple AI providers out of the box — OpenAI, Anthropic (Claude), Google Gemini, and OpenRouter — as well as any custom OpenAI-compatible endpoint. Highly customizable "skills" defined via simple Markdown files let you tailor Luma to your workflow.

---

## Features

* **Screen & Object Description:** Capture the entire screen, the current application window, or a specific navigator object and send it to a vision-capable AI model for description.
* **Clipboard Processing:** Process text or image content from the clipboard with any skill.
* **Camera Capture:** Capture a frame from your webcam and process it with a skill — great for describing physical objects, documents, or your surroundings.
* **Text Chat:** Have a back-and-forth conversation with an AI, optionally including a screenshot as context.
* **Customizable Skills:** Define reusable prompt templates as Markdown files with YAML frontmatter. Each skill can specify its own provider, model, and temperature.
* **Preset Providers:** OpenAI, Anthropic, Google Gemini, and OpenRouter are built in — just set your API key.
* **Custom Providers:** Add any OpenAI-compatible API endpoint (LM Studio, Ollama, etc.).
* **Custom Shortcuts:** Map any of 10 shortcut keys to a skill and scope combination for instant one-press execution.

---

## Getting Started

1. **Set Up a Provider:** Open NVDA menu > Tools > Luma > Providers. Under "Preset Providers", select a provider (e.g. OpenAI) and click "Set API Key..." to enter your key. You can also add a custom OpenAI-compatible provider at the bottom.
2. **Set Defaults:** Go to Tools > Luma > Settings and choose your default provider and model. Only providers with an API key configured appear here.
3. **Select a Skill:** Press `NVDA+Shift+Space` to cycle through available skills, or use the Active Skill submenu.
4. **Use It:** Press `NVDA+Shift+G` to describe the screen, or `NVDA+Shift+R` to describe the navigator object.

---

## Keyboard Shortcuts

| Gesture | Action |
| :--- | :--- |
| `NVDA+Shift+Enter` | Open the Luma popup menu |
| `NVDA+Shift+R` | Process the navigator object with the active skill |
| `NVDA+Shift+R` (double press) | Open text chat with the navigator object as context |
| `NVDA+Shift+G` | Process the entire screen with the active skill |
| `NVDA+Shift+G` (double press) | Open text chat with the screen as context |
| `NVDA+Shift+A` | Process the current application window with the active skill |
| `NVDA+Shift+A` (double press) | Open text chat with the application window as context |
| `NVDA+Shift+C` | Process clipboard content with the active skill |
| `NVDA+Shift+C` (double press) | Open text chat with clipboard content as context |
| `NVDA+Shift+L` | Capture a webcam frame and process with the active skill |
| `NVDA+Shift+L` (double press) | Capture a webcam frame and open text chat |
| `NVDA+Shift+T` | Open text chat |
| `NVDA+Shift+Space` | Cycle to the next active skill |
| `NVDA+Shift+1` to `0` | Execute a user-configured shortcut |

All gestures can be reassigned in the NVDA Input Gestures dialog under the "Luma" category.

---

## Providers

Luma supports two kinds of providers:

### Preset Providers

These are built-in and require only an API key to activate:

| Provider | API | Notes |
| :--- | :--- | :--- |
| **OpenAI** | OpenAI Chat Completions | GPT-4o, GPT-4.1, etc. |
| **Anthropic** | Anthropic Messages API | Claude Sonnet 4.6, Haiku, etc. |
| **Google Gemini** | Gemini generateContent API | Gemini 2.0 Flash, Pro, etc. |
| **OpenRouter** | OpenAI-compatible | Routes to many models |

Set your API key in Tools > Luma > Providers > "Set API Key...".

### Custom Providers (OpenAI Compatible)

For self-hosted or third-party endpoints that follow the OpenAI API format:

* **LM Studio** — local models
* **Ollama** — local models (Luma auto-detects Ollama URLs)
* Any other OpenAI-compatible endpoint

Add these via Tools > Luma > Providers > "New Provider...".

---

## Skills

Skills are Markdown files stored in:

* **Built-in:** `globalPlugins/luma/skills/builtin/` (shipped with the add-on)
* **Custom:** `globalPlugins/luma/skills/custom/` (user-created)

### Skill file format

```markdown
---
Name: Describe Screen
Provider: Default
Model: Default
Temperature: 0.3
Language: English
---
# Prompt
Describe this screenshot in detail for a blind user. Use {Language}.
```

The YAML frontmatter defines metadata; the body after `# Prompt` is the prompt template. Use `{Language}` as a placeholder that gets replaced at runtime.

You can also create and edit skills through the UI: Tools > Luma > Skills.

---

## Menu Structure

The Luma submenu appears under NVDA > Tools > Luma:

* **Providers...** -- Configure preset provider API keys and custom providers.
* **Skills...** -- Manage built-in and custom skills.
* **Process Navigator Object** -- Run the active skill on the current navigator object.
* **Process Selected File** -- Process the file selected in File Explorer.
* **Process Entire Screen** -- Run the active skill on a full-screen capture.
* **Process Current Application** -- Run the active skill on the current application window.
* **Process Clipboard** -- Process text or image from the clipboard with the active skill.
* **Process Camera** -- Capture a webcam frame and process with the active skill.
* **Text Chat** -- Open a conversational chat window.
* **Shortcuts...** -- Map `NVDA+Shift+1` through `0` to skill/scope combinations.
* **Settings...** -- Set the default provider, model, and output preferences.

---

## Settings

* **Default Provider / Model:** Used when a skill specifies "Default". Only providers with an API key configured are shown.
* **Open result in a browseable dialog:** When checked, results appear in a resizable dialog with a Copy button. When unchecked, results use NVDA's built-in virtual buffer.

---

## Requirements

* NVDA 2025.1 or later
* At least one AI provider with a vision-capable model

---

## License

This add-on is distributed under the GNU General Public License v2 or later.
