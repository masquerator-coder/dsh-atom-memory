# Scope-aware memory

Memory used to be one global pool per user. Every fact was equally available
everywhere, and *where a fact came from* — which project, which client, which
phase of the work — was an implicit property nobody recorded. That produced five
observable failures:

| Failure | What it looked like |
| --- | --- |
| Cross-project pollution | project A's rule was recalled while working on project B |
| A global rule that could not be told apart from a project's | "TypeScript strict mode on" was either bound to one project or lost in the noise |
| A project override destroying the general rule | a project's value *superseded* the company-wide one, for every project |
| Restating a claim in a second project | folded into the first as a duplicate — the evidence that a pattern is general was thrown away |
| An injection with no scope | the session-start digest mixed unrelated projects together |

This document describes what was built to fix them: the data model, how a
session's context resolves to a scope, what the write and read paths do
differently, and — because a dimension that cannot be repaired is a liability —
the management surface for fixing a tree that was built wrong.

`docs/memory-semantics.md` holds the behavioural policies as rules; this document
holds the design. Where a decision is non-obvious the *reason* is written down
next to it, because most of these decisions are only correct given the failure
they prevent.

---

## 1. The model

```
scope                 one node per (parent, type, name); materialised `path`
scope_alias           other names this node answers to
scope_signal          the fingerprints that identify it (unique per type+value)
scope_candidate       the low-confidence queue (not a scope yet)
fact_scope            facts <-> scopes (many-to-many)
fact_condition        "cross-project but conditional" experience
fact_origin           which concrete facts an abstraction came from
fact_evolution        how one fact became another (exception / evolves_to / ...)
```

Scope types, in increasing specificity — the order *is* the hierarchy:

```
global > user > org > team > client > project > series > phase > document > thread
```

**A fact with no `fact_scope` row is a global fact.** That single rule keeps the
read paths total (`unbound` is never "invisible"), gives every row written before
the dimension existed a meaning without inventing a binding, and is what the
compatibility clause in every scope-aware SQL fragment implements.

**`path` is a label, not a key.** The tree is walked through `parent_id`; the
materialised path (`/global/client:acme/project:github.com/acme/api`) exists for
display and debugging. Canonical names can themselves contain separators (a
remote URL, a folder path), so parsing `path` back into a tree would be ambiguous
the first time a name contained a slash.

**Identity is a signal, not a name.** A scope is identified by rows in
`scope_signal`, each unique on `(signal_type, normalized_value)`, so the same
repository reached by `git@github.com:Team/Repo.git`, `https://github.com/team/repo/`
and `ssh://git@github.com/team/repo.git` is one project — and two repositories
that happen to share a basename are two. `scope_alias` holds weaker equivalences
(a display name, a project code) that a human or an extractor asserted.

## 2. Signals and reliability

`atom_memory/context.py` owns the table. Reliability is *per signal type*, so a
caller cannot talk a weak signal into being strong by asserting it confidently.

| Signal | Reliability | Identifies |
| --- | --- | --- |
| `explicit_org` / `explicit_team` / `explicit_client` / `explicit_project` / `explicit_series` / `explicit_phase` | 0.95 | that level |
| `git_root`, `doc_id`, `folder_id`, `email_thread` | 0.90 | project / document / thread |
| `git_remote`, `org_domain` | 0.80 / 0.75 | project / org |
| `share_link` | 0.75 | document |
| `package`, `doc_title`, `project_code` | 0.55 / 0.50 / 0.50 | project / document |
| `path`, `folder_path` | 0.50 | project / document |
| `participants` | 0.45 | team |
| `content_anchor` (an extractor's `scope_hint`) | 0.25 | project |
| `name` | 0.15 | project |

Conditions (`fact_condition`) are the orthogonal axis: `language`, `doc_type`,
`audience`, `industry`, `stage`, `tool`, `vcs` — "this holds **when** writing a
proposal", not "this belongs to the proposal project". They are normalised the
same way (key-shaped, lower-cased) so `doc_type=Proposal` and `doc_type= proposal`
are one condition.

### Who supplies them (the dsh half)

The store can only see text, so the host observes the environment and sends the
result as the `scope_context` payload of each call. In the dsh plugin that lives
in `dsh/src/scope.ts`: the session's working directory (`path`), the git root and
`remote.origin.url` found by walking up from it (`git_root`, `git_remote` — a
worktree's `.git` *file* is followed to its git dir, and credentials are stripped
before the URL becomes a signal), and the declared package name (`package`, from
`package.json` or PEP 621 `pyproject.toml`). A deployment can add its own tags
(`explicit_org` / `explicit_client` / `explicit_project` / `explicit_series` /
`explicit_phase`), which is the cheapest way to make a single-purpose harness
resolve correctly on its first message.

Two properties matter for compatibility. Collection **never throws** — an
unreadable file is one fewer signal — and a payload with nothing in it is
**not sent at all** (not sent empty), so a deployment with no context keeps the
pre-scope params and therefore the pre-scope ranking. The filesystem facts are
cached per working directory, because they cannot change during a session and
every capture, tool call and prompt freeze asks for the same answer.

The extractor adds the other two fields per fact: `conditions` (*when* a claim
holds) and `scope_hint` (*where* it belongs — a hint that arrives as the
0.25-reliability `content_anchor` and can never bind a scope on its own).

## 3. Resolution

`ScopeStore.resolve(ctx, create)` is the single decision point. In order:

1. Walk the context's chain **most specific first**. For each level, look its
   signals up in `scope_signal` (exact identity), then in `scope_alias`
   (recorded equivalence). The first hit wins.
2. On a hit: register the newly observed signals that belong to that scope's own
   type, refresh `last_seen_at` up the ancestor chain, and return `bound` — or
   `pending_confirmation` for an alias-only hit whose confidence stays below
   `scope_bind_threshold`.
3. On no hit: create every level whose evidence reaches `scope_new_threshold`,
   general level first, so a new project is filed under its client rather than
   under the root.
4. Otherwise queue the sub-threshold hypothesis in `scope_candidate` and return
   `unresolved` — bound to the nearest *known* ancestor, so the fact is still
   reachable — or `global` when there is not even a hypothesis.

Three decisions in there are deliberate and easy to mistake for omissions:

* **A matched signal always binds.** The thresholds govern *creation* and how
  much confidence a binding *claims*, not re-identification. A `scope_signal`
  row is unique, so seeing one again is proof of the identity; a `path` (0.50)
  can only ever have been registered on a scope that stronger evidence — or three
  consistent sightings — already created.
* **A weak candidate is queued, not created.** A project known only by its folder
  path is created after `scope_promote_after` (default 3) *consistent* sightings,
  or when the user confirms it. Consistency is a database property: the unique
  index makes the same `(type, name, signal)` one row whose count rises, while a
  different folder path is a different row that cannot promote the first.
* **Resolution never restructures the tree.** If a matched scope sits under
  `/global` while the context also names a client, the client is not spliced in as
  its parent: doing that on a heuristic would silently move every fact already
  bound to it. `scope_reparent` exists for that correction, as an explicit act.

`MemConfig.scope_aware` is enforced **at `resolution_for`**, the one entry point
the automatic paths share, so "off" means the resolver cannot create anything.
The management methods deliberately ignore it: repairing a tree has to be
possible whether or not automation is on.

## 4. Writing

The write path resolves **once per batch** (one utterance comes from one place),
then for each candidate:

| Situation | What happens |
| --- | --- |
| same scope, same claim (fingerprint or embedding) | folded: the existing row is reinforced |
| same scope, single-valued key, different object | judged: the newer assertion supersedes, or is refused for weaker evidence |
| different scope, same object | a **second fact**, linked `cross_scope_similar` in `fact_origin` |
| different scope, different object, one scope an ancestor | both kept, linked `exception` in `fact_evolution` |
| different scope, different object, otherwise | both kept, linked `evolves_to` |

The dedup and conflict window is **exactly the write's own scope**, not its
ancestors. That is the load-bearing decision: were ancestors included, a project
stating its own value under a single-valued key would *supersede* the global rule
and the rule every other project needs would be gone. Recall expands ancestors
instead, so both statements come back with the project's ranked higher.

`replace` inherits the replaced fact's scopes: a correction must not move a fact
into whatever project the caller happens to be in now.

## 5. Reading

`Retriever.search(user_id, query, top_k, scope_context, conditions)`.

Candidate set = the scope's own path + its **phase** descendants + facts whose
conditions match the current context from anywhere else + unbound (global) facts.
A sibling project's fact is *not* a candidate; it can only enter through a
condition match, where it is then scored low. Anything else being present is
cross-project pollution, which is the thing this dimension exists to remove.

The re-rank adds three terms on top of the four base ones — **only** when the
query carries a scope context, so a scope-blind caller gets byte-identical
ranking to the pre-scope library:

```
score = 0.4·relevance + 0.2·effective_importance + 0.2·recency + 0.2·trust   (base)
      + 0.20·scope_distance_weight + 0.15·condition_match + 0.05·phase_match (scoped)
```

`scope_distance_weight`: current scope 1.0, parent 0.8, grandparent 0.6, global
0.5, another phase of the project 0.5, a sibling scope 0.3, anything else 0.15.

`condition_match`: 1.0 when every condition of the fact is satisfied by the
current context, `matched/total` in between, and **0.5 neutral** when either side
declares nothing — silence about conditions is not a mismatch.

`phase_match`: 1.0 for the current phase, 0.5 otherwise (a global rule is not a
phase mismatch; only an explicit disagreement is discounted).

## 6. The injected digest

The session-start snapshot is rendered as blocks instead of one flat list:

```
[当前项目: api] · 2 条
# 决策规则
- 提交前跑测试

[客户: acme] · 1 条
# 偏好
- 正式语气

[全局规则] · 3 条
...

-- 7 条事实 · 类型分布：决策规则 5 · 偏好 1 · 属性 1
```

* Each block has its **own budget**; the current scope gets the largest share and
  the global block has a reserved minimum, because "全局规则始终包含" is only true
  if a large project block cannot spend the reserve first.
* The artifact is capped **hard**. If everything does not fit, the overflow is
  taken back in reverse specificity order (condition rules → phases → distant
  ancestors → current scope → global rules last), then the footer (the one line
  that carries no memory), then whole blocks. An oversized digest is the failure
  mode a budget exists to prevent.
* Facts bound to a scope that is neither visible nor condition-matched are **not
  injected at all** — the design's "其他项目参考默认不注入". They stay reachable
  through `memory_recall`.
* The detail depth is deliberately *not* blocked: it exists to locate and edit
  facts, so it lists everything.

## 7. Promotion: a pattern, not a project's detail

A claim that three different scopes hold independently is the design's
cross-project reuse rule. The idle maintenance pass (`promote_abstractions`) finds
those claims by `content_fingerprint`, writes a copy at the root as
`system_inferred_high`, binds it to `/global` and links every concrete fact
through `fact_origin` (`relation='abstraction'`).

The concrete facts are **kept**: they are the evidence behind the rule, and the
place a project-specific nuance stays visible. Promotion is idempotent (a root
fact with the same fingerprint suppresses it), thresholded by
`scope_abstraction_min_scopes` (default 3), and writes the promoted row's FTS and
vector entries in the same transaction — a rule search cannot find is not a rule.

## 8. Managing the tree

```
scope_list(parent_id?, status?)      scope_resolve(user_id, scope_context, create?)
scope_create(type, name, parent_id?, signals?)   scope_alias_add(scope_id, alias, type?)
scope_confirm(scope_id)              scope_unresolved(user_id)
scope_merge(from_id, to_id)          scope_split(from_id, name, type, fact_ids)
scope_reparent(scope_id, parent_id)  scope_promote(user_id?)
fact_scope_bind(fact_id, scope_ids)  fact_condition_set(fact_id, conditions)
fact_scope_get(fact_id)
```

* **merge** folds one scope into another and keeps the source as
  `status='merged'`, `merged_into=<target>`; `resolve_id` follows the pointer, so
  history stays readable and every read path keeps working.
* **split** moves facts into a new child scope; the original keeps the rest.
* **reparent** is the correction merge and split cannot express — a project
  discovered before its client was ever named — and it is explicit precisely
  because resolution must not do it silently.
* **confirm** raises a scope's standing, which is what turns a promoted folder
  path into one that resolves without further doubt.

`memory_scope` is the model-visible front end for this surface (`dsh/src/tools.ts`):
`list`, `resolve` (the current resolution *plus* the unresolved candidate queue),
`create`, `confirm`, `alias_add`, `merge`. `resolve` is explicitly read-only — a
diagnostic call must not create a scope as a side effect — and the write path
remains the only place that creates scopes on its own.

## 9. Configuration

| Knob | Default | Meaning |
| --- | --- | --- |
| `scope_aware` | `True` | Master switch, enforced at the resolver |
| `w_scope` / `w_condition` / `w_phase` | 0.20 / 0.15 / 0.05 | The three scoped ranking terms |
| `scope_bind_threshold` | 0.9 | Bind and report the confidence as-is |
| `scope_pending_threshold` | 0.6 | Below this, an alias-only binding is marked unconfirmed |
| `scope_degrade_threshold` | 0.3 | Below this, no candidate is even queued |
| `scope_new_threshold` | 0.8 | Evidence a *new* scope needs (the 高 band) |
| `scope_promote_after` | 3 | Consistent sightings before a queued candidate becomes a scope |
| `scope_abstraction_min_scopes` | 3 | Independent scopes required to promote a global rule |
| `scope_all_phases` | `True` | Recall also considers the project's other phases |

## 10. Upgrading from a pre-scope database

Migration 011 **clears the fact store** (facts, their FTS/vector entries, their
reinforcement log and their candidate provenance). Scope binding cannot be
back-filled: nothing in an existing row says which project it came from, and
defaulting everything to global would silently re-create the cross-project pool
this change exists to remove. `user_profile` is untouched — since migration 010
it is a table the user owns and it is not derived from facts. This is a one-time
consequence of introducing the dimension, not a policy.

## 11. Tests

* `tests/test_scope.py` — signal normalisation, resolution (create / bind / queue
  / promote / read-only), hierarchy and expansion, distance and condition
  weights, scope-layered validation, merge / split / reparent, and the digest
  (block headings, hard budget, squeeze order).
* `tests/test_scope_writes.py` — the write path (filing, conditions, cross-scope
  links, same-scope supersede, `replace` inheritance), the abstraction pass, and
  the `AtomMem` + RPC surface for every scope method.
