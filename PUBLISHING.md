# Releasing Stowaway

Repositories:
- **https://github.com/Sat32blk/Stowaway**: Stowaway itself, plus the Unraid template (`unraid/`) and the Heimdall tile (`heimdall/`).
- **https://github.com/Sat32blk/Stowaway-homeassistant**: the Home Assistant integration. It's a separate repository because HACS requires one integration per repository.

## Docker image

Image and container names must be lowercase, so the image stays `ghcr.io/sat32blk/stowaway` even though the repository is called `Stowaway`.

`.github/workflows/docker-image.yml` builds the image for amd64 and arm64 and publishes it to `ghcr.io/sat32blk/stowaway`. Watch it under the repository's **Actions** tab.
- Every push to `main` updates `:latest`.
- A tag like `v1.2.0` also publishes `:1.2.0` and `:1.2`, and sets the version shown in Settings → Diagnostics.

**One-time step:** the first published image is private. Open github.com/Sat32blk → **Packages** → **stowaway** → **Package settings** → **Change visibility** → **Public**.

## Making a release

1. Bump `VERSION` in `app/version.py`.
2. Commit and push.
3. Run `git tag v1.2.0 && git push origin v1.2.0`.
4. On GitHub, open **Releases → Draft a new release**, choose the tag, and list what changed.

## Home Assistant integration

1. Bump `version` in `custom_components/stowaway/manifest.json`, then commit and push.
2. Publish a GitHub release (e.g. `v1.0.1`). HACS offers the update from releases.
3. The **Validate** action checks every push with Home Assistant's and HACS's validators, and runs the tests.
4. Optional: once it has had some use, open a pull request to `hacs/default` adding the repository to the `integration` file, so it's listed in HACS without a custom repository.

## Heimdall tile

Fork `linuxserver/Heimdall-Apps`, copy `heimdall/Stowaway` into it, and open a pull request.

## Never commit

The `config/` folder holds your account, API tokens, certificates and settings. `.gitignore` excludes it.
