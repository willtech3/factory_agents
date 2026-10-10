---
name: platform-contract
description: The contract between an application repository and the deployment platform (GCP project, CI templates on GitLab CI or GitHub Actions, per-branch preview environments, dev target). Loaded by the decomposer, spec writer, spec judge, implementer, and reviewer so that every work item is deployable through the existing platform without any agent authoring infrastructure.
---

<!-- PLACEHOLDER. The platform team already maintains the real version of this skill.
     Replace this whole file with it. Nothing below is contract content; it only lists the
     headings the factory's agents, rubrics, and checks expect the real skill to answer. -->

# Platform contract (placeholder)

The factory expects the real skill to answer, under headings of roughly this shape:

## What the platform provides
- The CI pipeline every branch gets (build, test, deploy stages) and what triggers a deploy.
- Per-branch preview environments: how the URL is derived, how long they live, how they are torn down.
- The `dev` target that the default branch deploys to, and how promotion beyond `dev` happens (by a human).
- Identity: the Workload Identity Federation (WIF) binding and service account GitLab CI runs as.
- Runtimes, base images, databases, secrets delivery, logging and metrics endpoints the app may rely on.

## What the application must define
- A **service manifest at a fixed path in the repository** (the factory validates it against
  `schemas/service_manifest.schema.json`; the platform team's schema replaces that file).
- The health endpoint the platform probes, and what "healthy" means.
- Declared environment variables (with secret flag) and the command that runs migrations.
- Any CI template includes or variables the repository must set.

## What agents must never touch
- IAM, service accounts, WIF bindings, and any permission grant.
- Networking: VPCs, firewall rules, DNS, load balancers, ingress.
- The platform repository, shared CI templates, and cluster or project configuration.
- Anything that promotes a build beyond `dev`.

## How the factory uses this skill
- The decomposer sizes and orders work so each item is deployable to a preview environment on its own.
- The spec writer declares the manifest changes a spec needs and writes `test_plan.e2e` entries that run against the preview URL.
- The spec judge scores S11 (deployability) and S12 (end-to-end coverage) against this contract.
- The implementer and reviewer run `test_plan.e2e` against the preview environment; neither edits anything listed under "must never touch".
