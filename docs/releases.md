# Releases and updates

HomeProxy makes a release for a new HAProxy patch, a security fix, a bug fix or a new feature. The last section is for contributors.

## Versions

A HomeProxy version has the form `X.Y.Z`. The image holds HAProxy and all other parts, so a new HAProxy comes only with a new HomeProxy version.

| Release | Version change | Examples |
| --- | --- | --- |
| Patch | `0.1.0` to `0.1.1` | A new HAProxy patch, a security fix in a part of the image, a bug fix |
| Minor | `0.1.1` to `0.2.0` | A new feature, a new HAProxy LTS branch, a new format of the config files |
| Major | `1.4.2` to `2.0.0` | A change that needs a step from you. The release notes tell you the step |

Before version 1.0.0, a minor release can also need a step from you. The release notes tell you.

Other updates of the parts in the image do not make a release. The next release includes them.

## Image tags

Each release publishes one image for amd64 and arm64 at `ghcr.io/georgegedox/homeproxy`. Each image gets the smoke test, the integration tests and the end-to-end tests on a machine of its architecture. The arm64 image has not run on a real arm64 home lab yet.

| Tag | Points to |
| --- | --- |
| `X.Y.Z`, for example `0.2.1` | That release. It never changes |
| `X.Y`, for example `0.2` | The newest patch release of `0.2` |
| `X`, for example `1` | The newest release of major version 1. This tag starts at 1.0.0 |
| `latest` | The newest release |

Select the tag in `HOMEPROXY_IMAGE` (see [Install](install.md#3-choose-your-settings)):

- To update by hand, use `latest` or an `X.Y.Z` tag.
- If a tool updates the container for you, use `0.Y`, for example `0.2`. From 1.0.0, use `1`. Then a release that can need a step from you does not install without you.

## Update notices

The Overview page shows the HomeProxy version. When a newer release is available, it shows that release. The `new_version` notification tells you about each new release one time (see [Notifications](notifications.md#events)).

The Overview page also shows the newest patch of the newest HAProxy LTS branch. This is for information only. That patch comes to you in a HomeProxy release.

To update, do the steps in [Install](install.md#update). The [releases page](https://github.com/GeorgeGedox/homeproxy/releases) lists the changes in each release.

## Go back to an earlier version

A release can change the format of the config files. At the first start, it changes your files to the new format. It keeps a copy of the initial files in `/data/migration-backups/<time>/`, with the same paths as in `/data`. An earlier release cannot read the new format.

If you go back to an earlier version and the newer version did not change the files:

1. Set `HOMEPROXY_IMAGE` to the earlier `X.Y.Z` tag.
2. Run `docker compose up -d`.

If the newer version changed the files:

1. Stop the stack with `docker compose stop`.
2. Copy the files from the newest directory in `/data/migration-backups/` back to the same paths in `/data`.
3. Set `HOMEPROXY_IMAGE` to the earlier `X.Y.Z` tag.
4. Run `docker compose up -d`.

The copied files do not include the changes that you made after the update.

If you start an earlier version with files in a newer format, the dashboard starts, but it cannot read the files. Its log shows a message such as `'config.yaml' has version 2, and this release supports 1`. Do the steps above to copy back the initial files.

## Verify an image

Each release image has a build provenance attestation from GitHub. It shows that the image came from this repository's release workflow. To verify an image, use the GitHub CLI:

```sh
gh attestation verify oci://ghcr.io/georgegedox/homeproxy:0.2.1 --repo GeorgeGedox/homeproxy
```

## How a release is made

[Renovate](https://docs.renovatebot.com/) keeps the versions of all parts up to date. Its settings are in `renovate.json`.

- Renovate opens its pull requests into `dev` on Monday mornings. It ignores a release that is less than 3 days old.
- A new HAProxy patch and a security fix do not wait. Renovate opens their pull requests at any time.
- A pull request merges by itself when all tests pass. These updates wait for the maintainer: a major version, also of a GitHub Action, a new HAProxy LTS branch and a new minor version of the CrowdSec SPOA bouncer. Renovate does not open a pull request for a new Python minor version, because Python follows the Debian version of the HAProxy image.

The type of each commit on `dev` sets the next release:

| Commit type | Release |
| --- | --- |
| `fix` | Patch release. It merges and publishes by itself when all tests pass |
| `feat` | Minor release. It waits for the maintainer |
| A `!` after the type, or `BREAKING CHANGE` | The maintainer makes this release by hand |
| Other types, for example `chore` or `docs` | No release. The next release includes the commit |

A HAProxy patch and a security fix come from Renovate as `fix` commits. Other dependency updates are `chore` commits.

Each day at 06:30 UTC, the `release-pr` workflow opens or updates the pull request from `dev` into `main`. The maintainer can also start it from the Actions tab and select a patch or minor release. This ships updates that came as `chore` commits, for example a Debian security fix. When the pull request merges, the `release` workflow does these steps:

1. It builds the image for each architecture on a machine of that architecture.
2. It tests each image with the smoke test, the integration tests and the end-to-end tests.
3. It pushes the tested images, adds the tags and attests the image.
4. It creates the GitHub release with the release notes.

From a new HAProxy patch to a published image takes about one day.
