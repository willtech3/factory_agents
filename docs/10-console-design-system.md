# The Console design system: the drawn floor

The Factory Console (`orchestrator/factory/web/static/`) is a single page of vanilla JavaScript and one stylesheet. This page is the contract for anyone who adds a screen or a component to it. It is short on purpose: the system is a handful of rules, one model of a run, one stylesheet of tokens and components, and a few JavaScript helpers that every view uses. [ADR-0012](adr/0012-console-design-system.md) records why.

## Three questions, in this order

Every screen answers these at a glance, top to bottom, and keeps everything else one hover or one fold away:

1. **Where is the run?** One line with seven stations. The same line, full on the run and gate pages and in miniature on every Runs row; the Board's columns are its four phases.
2. **Who is working, and why is the run in this state?** The animated crew figures stand on the line at the current station. A written sentence names them and how long they have been at it; while the run builds, a second line lists the work items and where each is. The story reads cause then effect, station by station.
3. **What does it need from me?** Orange ink appears only where a person must act: the figure that stands for you at an open gate, the "waiting on you" lead of a sentence, the one orange button.

## The one model of a run

Four phases group seven stations. The Board's columns are the phases; the line shows the stations; a gate is a station a person owns. `STATIONS` in `app.js` holds the stations' keys, names, owners and descriptions and `PHASES` the four phases; a gate's long, short and sentence forms live in `GATE_NAME`, `GATE_SHORT` and `gateLabel()`, and `summary.py`'s `GATE_LABEL` must change in step with them.

| Phase | Station | Key | Who |
|---|---|---|---|
| Plan | Outcome | `intake` | product owner |
| Plan | Decompose | `decomposing` | the crew (decomposer) |
| Plan | Gate 1 | `gate_1_decomposition` | product owner |
| Specify | Specify | `specifying` | the crew (spec writers, spec judge) |
| Specify | Gate 2 | `gate_2_spec_readiness` | product owner |
| Build | Build | `implementing` | the crew (implementer, reviewers, merge judge, integrator) |
| Ship | Pull requests | `completed` | engineer, then ops for the dev deploy |

Parked and terminal states (paused at budget, stopped, failed, escalated, done, closed) are never an eighth station. The line keeps the station the run reached; the sentence says what happened there (`stationOf()` finds it from the summary).

**Vocabulary.** One word per concept, everywhere: *Gate 1* and *Gate 2* (never "Station 3", "Plan check", "Contract check"); *the crew* for the agents together and the role name for a member (Decomposer, Spec writer, Spec judge, Implementer, Reviewer, Security reviewer, Merge judge, Integrator); *session* for one unit of a crew member's work (counted next to cost; the raw list stays in the fold); *pull request*; *work item* for the unit of work and *contract* for the specification an agent builds to; *Needs you* for the status, *Your turn* for the panel; *Done* (never "completed") in anything a person reads; *Board* for the page and its route.

## Rules of the look

- **Paper on the floor.** Sections are white paper (`.card`) on the cool floor (`--bg`), no border. Inside paper, groups are separated by hairlines (`--hair`), never by boxes inside boxes.
- **No pills, badges, chips, or coloured bars.** A status is a word in ink (`.tag` is plain bold text; its tone classes only colour it). Nothing is drawn behind a label. No coloured left or top borders.
- **No role colours.** "Product owner", "Engineer", "Ops" are words. The crew is told apart by its figure, not by a hue.
- **One meaning per ink.** Orange (`--call-text`, `--call-solid`, the `you` figure) means a person is needed now and nothing else. Clay (`--crew`, `--crew-text`) is the crew. Green, amber and red inks (`--ok-text`, `--warn-text`, `--bad-text`) are text only, for done, parked, and broken. Every text ink (`--text` to `--text-3`, `--link`, the `*-text` tones) is at least 4.5:1 on every ground in both themes and the figure fills (`--call`, `--crew`, `--human`) at least 3:1; `tests/test_experience_review.py` checks every one. `--text-4` is for what is ahead or disabled, never for reading.
- **Status is written, not coded.** A run's state is a sentence in the display face with a bold lead (`statusSentence()`), then a grey meta line (`metaLine()`). Never a bare "30 s" or an absolute clock time without a relative one.
- **Times are relative on the page, absolute on hover.** Use `timeEl(iso, text?)`; it renders `<time datetime title="…">4 min ago</time>`. The one place with a clock on the page is the raw event fold, which shows the viewer's clock with the date on hover.
- **One slot, one unit.** A row's right-hand figure is elapsed time while a crew member works and cost once it is done, never both in the same slot.
- **Nothing is cut mid-word.** Long agent prose goes through `firstSentences(text, max)`, which ends at a sentence or adds an ellipsis at a word boundary, and the whole text stays one fold or one link away.
- **Monospace is for identifiers.** Run ids, spec ids, hashes, commands. Never for labels.
- **Motion carries meaning.** The belt moves and the figures bob and tap only while the crew works. Awake figures blink and sleeping ones breathe their z's; the figure that stands for you bobs because it is waiting; a recorded decision pops once. Nothing else moves. Everything respects `prefers-reduced-motion`, and the sidebar's Hold still switch stops it all.

## Tokens (`app.css`, section 1)

Components use only semantic names. Add a primitive only when a semantic name needs it.

| Group | Tokens |
|---|---|
| Grounds and rules | `--bg` (floor) · `--surface` (paper) · `--surface-2` (one quiet inset) · `--hair` (divider inside paper) · `--line`, `--line-2` (inputs, buttons, the track ahead) · `--rule` (a section's top rule, the active tab) · `--belt`, `--belt-2` |
| Ink | `--text`, `--text-2`, `--text-3` (labels and meta) · `--text-4` (only what is ahead or disabled) · `--ink` · `--link` |
| Tone inks | `--call-text`, `--call-solid`, `--call` · `--crew`, `--crew-text` · `--human` · `--ok-text`, `--warn-text`, `--bad-text` |
| Space | `--sp-1` … `--sp-8` = 4, 8, 12, 16, 20, 24, 32, 40 px |
| Type | `--fs-xs` 12 · `--fs-sm` 13 · `--fs-md` 14 · `--fs-base` 15 · `--fs-lg` 17 (section titles) · `--fs-xl` 21 (the sentence) · `--fs-2xl` 30 (the page title) · `--display`, `--sans`, `--mono` |
| Shape | `--radius` 10 (paper) · `--radius-ctl` 6 (buttons, inputs) · `--radius-nav` 8 |
| Layout | `--w-side` 200 · `--w-content` 1440 · `--w-aside` 340 · `--w-roster` 300 |
| Motion | `--t-fast` 120 ms · `--t-base` 160 ms for transitions · `--t-blink` 4.2 s · `--t-bob` 1.6 s · `--t-tap` 1.1 s · `--t-belt` 1.2 s for the crew and the belt |

Dark mode redefines the same names under `html[data-theme="dark"]`, the fills (`--ok`, `--warn`, `--bad`) and the two orange pairs (`--call-solid-fg`, `--call-on-ink`) included; a component never branches on the theme.

## Components (`app.css`, sections 3 to 10)

| Component | Class | Notes |
|---|---|---|
| Page head | `.page-head` with `h1`, `.sub`, `.crumbs`, `.actions` | `pageHead(title, sub, actions, crumbs)` |
| The sentence | `.now .sentence`, `.now .meta` | the run page header; `statusSentence(summary, true)` and `metaLine(summary)` |
| Label | `.tag`, tones `.call .crew .ok .warn .bad` | a word in ink; no ground |
| Button | `.btn`, `.btn.primary`, `.btn.call` / `.btn.approve`, `.btn.danger`, `.btn.changes`, `.btn.link`, `.btn.lg`, `.btn.sm`, `.btn.block` | ink-solid for the main action of a screen, orange-solid only for a decision, hairline for the rest |
| Filters | `.chips` with `.btn[aria-pressed]` and `.n` | text tabs with counts; the active one carries the rule |
| Section | `.card`, `.card-head` (`h2`, `.meta`), `.card-body` | `card(title, body, meta, cls)`; `.card.soft`, `.card.info` and `.card.call` render as plain paper |
| Key-value list | `.kv` | |
| Notice | `.notice` with `.ok .warn .bad .info` | a sentence between two hairlines, the lead `<b>` carries the tone |
| Table | `table`, `th`, `td` | hairline rows, grey heads, no vertical rules |
| Row list | `.list > li` with `.main`, `.title`, `.desc` | |
| Runs list | `.runs-list > .run-row` with the mini line, `.what` (`.t`, `.s`), `.meta` | one row per run |
| The line | `.line` (`.belt`, `.stations > .st.done/.current/.ahead/.call` with `.figs`, `.post`, `.lbl`, `.d`), `.line.mini` | `lineStrip(record, "full" or "mini")`; the figures stand in `.figs` |
| Crew figure | `svg.crew` with states `asleep working done blocked still` | `crewFigure(role, state, "l" / "s" / "xs", label)`; a `label` makes it `role="img"` |
| Human figure | `svg.human.you` / `svg.human.decided` | `humanFigure(kind, size, label)` |
| Roster | `.roster > .m.working/.done/.asleep/.blocked/.interrupted` | figure, name, one line, elapsed or cost; five states, `interrupted` for a session the controller cut short |
| Story | `.story > .grp` and `.e` (`time`, `.body .t .who`, `.d`) | `storyEntries(run)` returns entries with `group` = station key |
| Your turn | `.turn` with `.lead-line`, `.next`, `.item` | sentences and hairlines; one button per item |
| Board | `.board .g` (150px + four phase columns), `.colh`, `.rowh`, `.cell`, `.k` / `.k.agent` | rows are owners, columns are phases |
| Gate review | `.review` (document + `.decide` aside), `.glance`, `.checklist`, `.item-card`, `.issue`, `.summary-card`, `.brief` | the numbers row has no boxes; an item is a rule-separated section |
| Question | `.ask` | paper, a bold question, the orange button |
| Fields | `.form .field`, `label.lab`, `.help`, `.field-error`, `.choice`, `.guards`, `.tier` | |

## Helpers (`app.js`, the top of the file)

`el(tag, attrs, …children)` builds DOM; agent-written markdown goes through `md()` (which escapes) before the `html:` attribute, never raw. `timeEl(iso, text?)`, `ago(iso)`, `dur(from, to)`, `money(n)`, `plural(n, one, many)`, `firstSentences(text, max)`, `crewNames(roles)`, `crewNamesLower(roles)`, `personName(actorId)` (hides `console`, `human` and other system actor ids), `gateLabel(kind)`, `stationOf(summary)`, `lineStrip(record, size)`, `crewFigure(…)`, `humanFigure(…)`, `statusSentence(summary, long)`, `metaLine(summary)`. New wording for an event kind goes into `storyEntries()` (the story) and `describe()` (the raw event list); both name the cause or the effect in plain words.

## Adding a screen

1. Start from `pageHead()`; if the screen is about one run, put its sentence under the title and the line in a `.card.line-card` right below.
2. Lay content out as paper sections on the floor; inside a section, separate groups with hairlines.
3. Write status as a sentence with a bold lead. Reach for a `.tag` only when a single word next to a title is enough, and never give it a ground.
4. Use the tokens. An inline `style` attribute is a smell: if a rule is needed twice, it is a class in `app.css`; if it is needed once, it is still a class.
5. Disclose in layers: glance (sentence, line, counts), hover (`title`, `timeEl`), expand (`<details>`), drill in (a link to the gate page, the session trace, the pull request).
6. Check both themes and a 390 px viewport. Nothing scrolls sideways; wide tables scroll in their own box.
7. If the screen names a stage, a gate, or a role, take the word from `STATIONS`, `GATE_NAME`, `CREW_NAME` or `ROLE_NAME`.

The standalone gate page (`surfaces/artifact_pages.py`) keeps a copy of the inks in its own `_CSS` so it stays a single file; change both when a token changes.
