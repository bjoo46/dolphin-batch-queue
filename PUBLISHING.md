# Publish to Comfy Registry

1. Sign in at https://registry.comfy.org and create a publisher. The Publisher ID is permanent.
2. Put the actual ID in `[tool.comfy].PublisherId` in `pyproject.toml` and commit the change.
3. Create a Registry publishing API key for that publisher. Add it to this GitHub repository's Actions secrets as `REGISTRY_ACCESS_TOKEN`. Do not put it in a file or commit it.
4. Run the **Publish to Comfy Registry** workflow manually from GitHub Actions.
5. Check the workflow result and Registry listing before announcing Manager availability. New versions require a new semantic version in `pyproject.toml`.

The workflow refuses to publish while PublisherId is empty. Tests run before publishing. `.comfyignore` excludes development files from the Registry archive; `.gitignore` excludes local queue state.

Official guide: https://docs.comfy.org/registry/publishing
