# Luma - AI Companion for NVDA

**Luma** is an NVDA add-on that brings AI-powered visual description, screen analysis, and conversational chat to blind users. It supports multiple AI providers out of the box — OpenAI, Anthropic (Claude), Google Gemini, and OpenRouter — as well as any custom OpenAI-compatible endpoint. Highly customizable "skills" defined via simple Markdown files let you tailor Luma to your workflow.

---

## Features

* **Screen & Object Description:** Capture the entire screen, the current application window, or a specific navigator object and send it to a vision-capable AI model for description.
* **Clipboard Processing:** Process text or image content from the clipboard with any skill.
* **Camera & Scanner Capture:** Capture a frame from any connected webcam or scan a page from a flatbed/document scanner and process it with a skill — great for describing physical objects, printed documents, or your surroundings.
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
| `NVDA+Shift+L` | Pick a webcam or scanner, capture an image, and process with the active skill |
| `NVDA+Shift+L` (double press) | Pick a webcam or scanner, capture an image, and open text chat |
| `NVDA+Shift+T` | Open text chat |
| `NVDA+Shift+I` | Open Interaction Mode (AI computer control) |
| `NVDA+Shift+K` | Toggle Live mode (continuous change-only narration) |
| `NVDA+Shift+K` (double press) | Ask a question that Live mode answers continuously |
| `NVDA+Shift+F4` | Stop the current request or interaction |
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

## Live Mode

Live mode takes a screenshot every two seconds and speaks only what changed, like a live audio describer. Press `NVDA+Shift+K` to start and again to stop. A rising beep marks the start, a falling beep the stop.

### Live questions

Double-press `NVDA+Shift+K` to open the *Luma Live — Ask a Question* dialog. Type or dictate what you want to know, for example *"Give me the slide title and a short description of the slide in the shared screen"*, and press Enter. The dialog hides, and Live mode now answers exactly that question: very concisely, and only when the answer changes (a new slide, a new value, a new state). While nothing changes you hear nothing.

* Every answer is spoken and also written to the dialog's Answers log. Double-press `NVDA+Shift+K` again to bring the dialog back, read the log, change the question (Enter restarts with the new question) or press Stop.
* A single press of `NVDA+Shift+K` stops Live mode at any time.
* Closing the dialog also stops a running live question.
* Live mode uses the built-in "Live" skill, so the provider and model come from that skill (or your defaults).

---

## Interaction Mode

Interaction Mode lets the AI operate the computer for you: you describe a task ("Check the Privacy checkbox and press OK"), and the model looks at screenshots, clicks, types and presses keys until the task is done. Every step is announced, and `NVDA+Shift+F4` (the stop gesture) cancels at any time. This is especially useful for inaccessible applications.

Open it with `NVDA+Shift+I` or from the Luma menu ("Interaction Mode").

### Supported providers

| Provider | How it works |
| :--- | :--- |
| **Anthropic** | Native Computer Use tool. |
| **Google Gemini** | Native computer-use tool. |
| **Custom provider with Interaction Mode enabled** | Luma drives any OpenAI-compatible vision model (vLLM, SGLang, llama.cpp server, LM Studio, OpenRouter…) through a prompted computer-use protocol. Recommended models: **Qwen3-VL** (best open GUI grounding), Qwen2.5-VL, UI-TARS. |

### Using a self-hosted model (e.g. Qwen3-VL on vLLM)

1. Tools > Luma > Providers > "New Provider..." (or "Edit Provider..." on an existing one).
2. Enter the OpenAI-compatible base URL (e.g. `https://ai.example.com/v1`) and your key.
3. Check **"Enable Interaction Mode"**.
4. Leave **Coordinate format** on *Auto* unless you know better. Auto reads the model name: Qwen2.5-VL and UI-TARS get *Screenshot pixels*, Qwen3-VL and Gemini-style models get the *Normalized 0–1000 grid*. Unknown names use the normalized grid; override manually if clicks land off-target.
5. In Tools > Luma > Settings choose this provider as **Interaction Provider** and set the **Interaction Model** (e.g. `Qwen/Qwen3-VL-32B-Instruct`). If you leave the Interaction Provider on auto-detect, the custom provider is used when it is also your default provider.

Tips:

* The server does not need tool-calling enabled; Luma embeds the tool protocol in the prompt. If the server does parse tool calls (vLLM `--enable-auto-tool-choice`), that is used automatically.
* Screenshots are sent as JPEG at up to 1920 px on the long side with the real aspect ratio, because small controls such as taskbar icons are lost when the image is shrunk. Only the three most recent screenshots stay in the conversation, so a long task fits a 32k context window and typical request-size limits. If the server still rejects a request as too large, Luma retries once with a single screenshot. Power users can change the long side with an `"image_long_side"` key on the provider entry in `luma.json`.
* **Refine clicks** (on by default) makes every click a two-step process: the model names its target, Luma cuts out the area around the proposed point from the full-resolution screen, enlarges it, and asks the model for the exact position of that element. This costs one small extra request per click and greatly improves accuracy on small controls. Turn it off in the provider dialog if your endpoint is slow.
* Before each action Luma announces exactly what it is about to do, e.g. *Left click on "Chromium icon in the taskbar" at 1560, 1015* or *Typing: chromium*.
* If clicks land consistently off by the same ratio, the model probably uses the other coordinate format — switch it in the provider dialog.

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
* **Process Camera or Scanner** -- Pick a connected webcam or scanner, capture an image, and process with the active skill.
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
