![dictation-vibecode-tuning — empirical tuning log for a fully-local voice-coding pipeline](docs/banner.png)

# dictation-vibecode-tuning

Tuning a **fully-local** dictation pipeline — on-device speech-to-text plus a
local-LLM enhancement pass — for a developer who dictates a mix of prose and the
occasional command, path, flag, or list to an AI coding assistant.

This repo is less "here's a tool" and more **"here's a multi-week empirical
tuning loop and what it found."** The scripts are the instruments; the findings
are the point.

> **Not affiliated with VoiceInk.** [VoiceInk](https://tryvoiceink.com) is a
> separate commercial macOS app by Prakash Joshipax. Nothing here is a fork or a
> redistribution of it. This is unofficial tooling and notes built *around* it —
> the same ideas apply to any STT-plus-local-LLM dictation setup.

## The stack

| Layer | Choice | Why |
|---|---|---|
| Speech-to-text | Parakeet V2 (`parakeet-tdt-0.6b-v2`), on-device | ~0.09s, effectively free, never the bottleneck |
| Enhancement | `gemma4-e4b` on a dedicated **llama-server** (llama.cpp), local | punctuation / fillers / formatting cleanup, no cloud, no API cost |
| Enhancement prompt | custom **"Vibe Coding"** v3.9, system-template OFF | the actual tuned artifact ([`prompt/vibe-coding.md`](prompt/vibe-coding.md)) |

Everything runs on-device. No API keys, no per-token cost, no audio or text
leaving the machine — which is the whole reason for doing the enhancement with a
local model instead of a cloud one.

## What's in here

```
prompt/vibe-coding.md          the tuned v3.9 enhancement prompt + extract script
scripts/voiceink-review.py     read-only reader of the SwiftData store: RAW vs ENHANCED pairs
scripts/voiceink-model-ab.py   controlled A/B of enhancement models at the enhancement layer
scripts/voiceink-prompt-ab.py  controlled A/B of two prompts at a fixed model
scripts/voiceink-server-ab.py  controlled A/B of llama-server flags (shadow server, see finding 5)
launchagents/                  LaunchAgent running the dedicated, always-resident llama-server
examples/                      illustrative: applying a new prompt programmatically
docs/architecture.png          pipeline & tuning system diagram
```

## Pipeline

![Three-layer dictation pipeline: VoiceInk captures audio → Parakeet V2 STT produces a RAW transcript → gemma4-e4b on a local llama-server with the Vibe Coding v3.9 prompt produces ENHANCED text → voiceink-review.py reads RAW/ENHANCED pairs for the miss-taxonomy review loop](docs/architecture.png)

> **Note:** `docs/architecture.png` still shows the earlier Ollama-based backend; the text diagram below and the findings reflect the current dedicated-llama-server setup.

<details>
<summary>Text version</summary>

```
[VoiceInk (audio capture)]   [LaunchAgent: dedicated llama-server, always resident]
          │                              │
          └──────────────┬───────────────┘
                         ▼
              [Layer 1 — STT: Parakeet V2]
                on-device · ~0.09s
                → RAW transcript
                         │
                         ▼
          [Layer 2 — Enhancement: gemma4-e4b]   ◄── voiceink-model-ab.py
              llama-server · local · Vibe Coding v3.9   (A/B harness)
                → ENHANCED text
                         │
                         ▼
              [Layer 3 — Review]
              voiceink-review.py
              RAW vs ENHANCED · 3-layer miss taxonomy
```

</details>

---

## Findings

### 1. The three-layer miss taxonomy

The single most useful idea in this whole exercise: **when a dictation comes out
wrong, the error lives in exactly one of three layers**, and conflating them
sends you tuning the wrong thing.

1. **Transcription miss** — the STT model genuinely misheard the audio. No prompt
   change can fix this; it's an acoustic/model problem.
2. **Enhancement divergence** — STT heard it right, but the enhancement LLM
   rewrote, over-compressed, or mis-formatted it. *This* is what prompt tuning
   addresses.
3. **Correct** — the output is faithful (including legitimate cleanup like filler
   removal or punctuation).

`scripts/voiceink-review.py` exists to make this split visible: it pulls the RAW
transcript (`ZTEXT`) and the ENHANCED output (`ZENHANCEDTEXT`) side by side from
the store, so every judgment call starts from "did the model mishear it, or did
the prompt mangle it?" Tuning without that split means guessing.

### 2. The thinking-token trap — and why it drove the backend choice

Every `gemma4` model is a *thinking* model: left to itself it silently generates
reasoning tokens before every reply. For a dictation-enhancement pass that is
pure overhead — you wait seconds for "thinking" you never see, and some serving
paths return *empty* visible content entirely.

The trap is that **it isn't fixable at the app layer.** VoiceInk sends no
`think:false` and offers no toggle ([open feature request
#589](https://github.com/Beingpax/VoiceInk/issues/589)), and on Ollama a
non-thinking variant can't be baked out via Modelfile (on 0.30.6 the `gemma4`
renderer is auto-assigned from the architecture and un-removable, and
`PARAMETER think false` is rejected). This is a large part of why enhancement
moved onto a **dedicated llama-server**: there, `--reasoning off` suppresses the
thinking tokens *server-side and unconditionally*, independent of what the app
sends. (Careful: the per-request `--chat-template-kwargs {"thinking":false}`
does **not** stop generation — it only reroutes the tokens to
`reasoning_content`. You need `--reasoning off`.) This applies to `gemma4-e4b`
itself, not just the big model — it's what makes the whole gemma4 path usable.

With thinking handled, is a *bigger* model worth it? No. An n=20 offline A/B put
`gemma4:12b` near-even with e4b (9 ties / 5 each / 1 toss-up). Its *losses* were
correctness-level: on natural-language dictation it over-renders prose into
symbols (the spoken words "slash commands" / "dash" become `/commands` / `--`)
and invents punctuation. Its wins were only nice-to-haves (kept list digits,
normalized identifiers). And it runs ~1.6x slower. So the small model isn't a
compromise — for this task it's the *correct* choice, and the experiment is what
proved it rather than assumed it.

### 3. The latency you feel isn't inference — and on a dedicated server it isn't idle-unload either

If local dictation feels slow, the instinct is "the model is too big." Wrong
layer. STT (Parakeet) is ~0.09s and never the problem, and warm enhancement is
~0.40s.

On an Ollama backend the felt stall was a **cold-load**: the model unloaded on
Ollama's idle timeout and reloaded (3-10s) on the first dictation after a gap.
Running enhancement on a **dedicated llama-server** removes that failure mode
outright — llama-server holds the model (`gemma-4-E4B-it-Q4_K_M.gguf`, 4.6 GB on
disk) resident indefinitely, with no idle TTL. What's left are three narrower
causes:

- **Sleep/swap re-fault.** After the machine sleeps, the mmap'd weights get paged
  out; the first post-wake dictation re-faults them from disk. Fix: `--mlock`,
  which pins the weights in RAM so they survive sleep/wake.
- **Decode-bound long outputs.** At ~65 tok/s a 200+ token cleanup is genuinely
  3-4s of generation — not a load stall, just work. This is the dominant term for
  a long dictation (~89% of wall-clock), and it is the one that responds to
  speculative decoding — see finding 5.
- **Prefix-cache misses.** Injecting variable context *before* the stable system
  prompt busts the prefix cache; keep the long, fixed Vibe Coding prompt first.

The other half is **isolation**: run enhancement on its own llama-server on its
own port (`:11435`), separate from any swap-on-demand pool (e.g. llama-swap on
`:11434`). Then no other model request can ever evict the enhancement model —
the same insight the old `OLLAMA_MAX_LOADED_MODELS=2` was reaching for, made
structural. (Shipped as a LaunchAgent in [`launchagents/`](launchagents/) so it
survives reboots.)

### 4. ...and a smaller model is the *wrong* fix for latency

The tempting shortcut to "fix" the stall — drop to `gemma4:e2b` — was tested and
rejected. e2b is ~1.7x faster *per token*, but: (a) that speedup is invisible
once the model is resident, (b) it does nothing for the stalls you actually feel
(sleep re-fault, long-output decode — see finding 3), and (c) it drops clauses
and corrupts numbers. The right lever was keeping e4b resident and `--mlock`'d,
not trading accuracy for a speedup you can't perceive.
`scripts/voiceink-model-ab.py` is the harness that measured this —
cold-load vs warm latency and tokens/sec per model, over a fixed set of real
rows, with the live prompt held constant so the model is the only variable.

### 5. Speculative decoding is nearly free here — and benchmarking it is booby-trapped

Enhancement is a **near-copy task**: most output tokens already appear verbatim
in the input transcript. That is the ideal case for *prompt-lookup* speculative
decoding, which drafts candidate tokens from the current context instead of from
a second draft model. Adding `--spec-type ngram-simple` (n=3, m=32) measured
**2.1x decode throughput** (~65 → ~135 tok/s) on novel dictations. On a ~45s
dictation that moved the enhancement pass from ~1.69s to ~0.84s median.

It is also **lossless**, and that claim is checked rather than assumed: at
temperature 0 the output is byte-identical to non-speculative decoding. A
speculative decoder that changes output is broken, so this is a hard gate, not a
nice-to-have.

**The trap.** A different variant, `--spec-type ngram-mod`, measured ~4x and was
shipped first. It was worth **0%**. `ngram-mod` keeps a *persistent cross-request*
n-gram store, and the benchmark re-sent the same transcripts to get a median — so
the server was allowed to memorise its own previous output. On first-exposure
text (i.e. real dictation, which is never a repeat) it scored 99.5% of baseline.
`ngram-simple` builds its index from the current prompt only, which is exactly
why it survives contact with novel input.

A second trap sat underneath the first: **ambient GPU load on the machine drifted
~2x between sequential runs**, which aliased onto whichever config happened to run
first and inverted the rankings outright.

So the method matters more than the number. Three rules, now enforced by
[`scripts/voiceink-server-ab.py`](scripts/voiceink-server-ab.py):

1. **Shadow server.** Candidates run on a scratch port; the production server is
   never reconfigured, so dictation keeps working during a sweep.
2. **First exposure.** Every transcript is seen exactly once per config. This is
   what kills the persistent-store artifact.
3. **Same-round baseline.** Configs are round-robined and each is scored against
   the baseline measured *in its own round*, so an ambient slow patch hits
   baseline and candidate equally and cancels out.

Two smaller results from the same sweep: `--swa-full` is required because gemma
uses sliding-window attention — without it llama.cpp cannot restore SWA KV state,
so any system-prompt switch logs `forcing full prompt re-processing due to lack of
cache data` and re-ingests the entire prompt (~1.9s spikes). And **q8_0 KV cache
made generation slower**, not faster (1.48x vs 1.64x on an identical spec config);
the dequant overhead outweighs the bandwidth saving at this context size, so the
cache is deliberately left at f16.

---

### 6. A prompt section can hijack a rule it never mentions — and a substring test can't see it

The prompt was silently **dropping whole sentences**. A three-sentence narrative
dictation ending in a request came back as only the closing request; the two
sentences of context in front of it were gone. Reproducible 3/3 at temperature 0,
so not sampling noise — and it had been live for weeks.

**The cause was not the rule you would guess.** Bisecting the prompt section by
section: the culprit is the **"Commands and code" section, and its ~130-character
header alone is enough to trigger it** — the body of the rules is not required.
Its mere presence flips the model into treating narrative dictation as a command
to be *restated*, so instead of cleaning "I told them X, and they asked me for Y,
can you draft it?" it emits "I want you to draft Y for them." A Prose rule
elsewhere in the prompt already forbade exactly this, and was being overridden.

Two checks kill the obvious alternative explanations: a **neutral filler block of
the same length** does not trigger it (so it is not context length or attention
dilution), and neither the Lists nor the Examples sections trigger it (so it is
not "one more section").

**Two fixes that sound right were measured and rejected.**

1. *Scope the offending section* — add a sentence telling the model those rules
   apply only to actual commands. **Zero effect.** This is the same negative
   carve-out failure documented in finding 1: a small model follows the
   imperative and drops the exception.
2. *Show it the correct shape* — add a worked example of a narrative dictation
   cleaned correctly. It **overfit**: it fixed the constructed test case and
   **failed a real held-out dictation** pulled from the transcript store.

That second result is the transferable one. Constructed test cases alone would
have shipped the wrong fix with a green board. Hold out real recorded dictations.

**What actually worked was 74 characters** appended to the prompt's closing line —
a positive, unconditional invariant rather than a scoped exception:

```text
Output nothing but the formatted text. Every sentence of the input appears in
the output, in the original order.
```

**The regression gate was blind to all of this, structurally.** Controls were
scored by checking that one substring survived enhancement. On this case the
control's needle happened to sit *inside* the one sentence the model kept — so
the gate scored a two-thirds truncation as a **pass**. Every single-substring
control shares this shape: it proves a fragment survived, never that the rest of
the transcript did.

The fix is to make each control carry a **list** of needles that collectively span
every content clause of the raw transcript, and pass only if **all** of them
survive. Authoring rule: needles must not cross a sentence boundary, because the
enhancer legitimately rewrites boundary punctuation. Re-checked in both
directions afterwards — the corpus passes on the fixed prompt, and the *old*
prompt now fails the case it used to pass, naming the clauses it dropped.

**Generalised:** a regression test that asserts on presence can only catch
corruption, never deletion. If the failure mode you care about is the model
*dropping* content, the assertion has to be anchored to the whole input.

---

## A worked example

The clearest enhancement-divergence case, and the one a specific prompt rule was
written to kill. An early prompt version collapsed a full question down to a bare
command:

```
RAW:       Can you run slash clear, then show me the git status?
BROKEN:    /clear
v3.6:      Can you run /clear, then show me the git status?
```

The first output isn't a transcription miss — STT heard every word. It's the
enhancement model "helpfully" reducing a sentence to the command embedded in it.
The fix is the explicit Lists rule in v3.6: *"never reduce an item to an embedded
command, flag, or path... keep the surrounding prose."*

Two more real pairs from the loop, one per layer:

**Enhancement divergence — over-compression (a v3.5 weakness targeted by the v3.6 Prose rule):**

```
RAW:       I'm reading a tweet about Opus 4.8 and there are some prompts example prompts in it to that I want to save somewhere.
ENHANCED:  Save the prompt examples from the tweet about Opus 4.8 somewhere.
```

STT heard every word. The enhancement model turned an observation ("I'm reading
... that I want to save") into a bare imperative and dropped content. This is the
layer prompt tuning targets — and a reminder that "cleanup" and "rewrite" sit on
a spectrum the model doesn't always get right.

**Correct — cleanup done right (layer 3):**

```
RAW:       If my obsidian inbox keeps getting full ... check on my inbox and and then in the morning ... discard them, you know, something like that.
ENHANCED:  ... check on my inbox and then in the morning ... discard them, something like that.
```

The doubled "and and" collapsed, the "you know" filler dropped, and nothing
substantive lost. This is what the enhancement pass is *for*.

---

## Reproducing this for your own setup

1. Install VoiceInk and set Parakeet V2 for transcription. For enhancement, run a
   local **llama-server** serving `gemma4-e4b` (start it with `--reasoning off`
   and `--mlock` — see finding 2 and 3), and point VoiceInk's **Custom** provider
   at `http://127.0.0.1:11435/v1/chat/completions` (model name `gemma4-e4b`).
2. Paste [`prompt/vibe-coding.md`](prompt/vibe-coding.md)'s prompt into a custom
   enhancement prompt, with **"Use System Template" OFF**.
3. Run the enhancement server as a LaunchAgent (see
   [`launchagents/`](launchagents/)) so it stays resident across reboots, on its
   own port isolated from any other model pool.
4. To run the review loop: `python3 scripts/voiceink-review.py status` (config via
   env vars — see the script header).
5. To A/B two models: `python3 scripts/voiceink-model-ab.py model-a model-b`
   (set `VOICEINK_CASE_PKS` to row ids from your own store).

The scripts read VoiceInk's SwiftData store **read-only** and never write it.
Paths and the review window are env-configurable; defaults reflect the reference
experiment.

## License

[MIT](LICENSE) — covers the scripts, prompt, and writeup in this repo only, not
VoiceInk itself.
