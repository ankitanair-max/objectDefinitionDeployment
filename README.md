# objectDefinitionDeployment

Tooling and conventions for the SEAP Salesforce **Data-Dictionary deployment
pipeline**: Google Sheet (source of truth) → validate → package → deploy custom
objects/fields/tabs/FlexiPages to a Salesforce org, with post-deploy FLS,
object-permission, and record-page automation.

## Contents

| Path | What it is |
|------|------------|
| `.cursor/rules/` | Mandatory pipeline rules (`.mdc`) — naming, deploy-delta, blockers, FLS/object-perms, FlexiPage 2-column, IsDelete/WIP handling, sheet-write gating, JA→en_US translation, etc. |
| `.cursor/deployment_knowledge.md` | Deployment self-correction knowledge base (error → root-cause → fix lessons). |
| `sf-deploy/scripts/` | Python pipeline scripts (fetch/validate sheet, name fields, generate XML, build manifest, deploy, verify, grant FLS/object-perms/tab-visibility, FlexiPage generation, drift checks, translation, etc.). |
| `sf-deploy/README.md` | Pipeline usage notes. The canonical command remains `python scripts/prep_deploy.py --org … --tabs … --phase deploy`. |

## Security

Org credentials (`.sfhome/`, `.sfdx/`, `orgauth.json`, access/refresh tokens) and
build artifacts are intentionally **git-ignored** and must never be committed.
