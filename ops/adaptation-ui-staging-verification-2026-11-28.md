# Adaptation interface update, staging only

The project library now uses cards with a clear project title, update time,
search and creation actions. Preview screenplays retain their source scene
number and display time, interior/exterior, location and cast before the
action and dialogue. Outline episode headings, field labels, body text and
cliffhangers have distinct typography. Administrator step cards use the
label “API成本” followed by the existing amount.

## Validation before deployment

- All nine frontend check scripts passed. Preview regression covers source
  numbering, original scene context, changed nighttime/exterior settings,
  legacy setting strings and unknown context without inventing a day/location.
- Administrator regression verifies all six step cards keep the amount while
  removing estimate and pending-price wording from these cards.
- Browser preview checked the project library at desktop and mobile widths,
  the parallel original/preview screenplays and the outline reading hierarchy.
- Release guard checks reject production targets, backend changes and a
  candidate outside the reviewed source ancestry. `git diff --check` passed.

## Release scope

`ops/release_adaptation_ui.py` accepts only a pushed revision and the actions
`prepare` or `deploy`; its only deployment target is staging. The four runtime
assets are index.html, app.css, app.js and adaptation.js. It verifies the
staging baseline, builds from the existing image without network access,
runs its unchanged adaptation backend regression, waits for idle computation,
and backs up the staging files before switching. Runtime configuration,
mounts, ports, served asset hashes and the production container fingerprint
are checked. Deployment evidence is written under
`/opt/jubianbian-adaptation-ui-20261128-<commit>/deployment.json`.
