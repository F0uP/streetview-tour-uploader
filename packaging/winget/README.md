# winget package

Goal: `winget install F0uP.StreetviewTourEditor` installs the editor.

## First version (once, by hand)

1. Publish a release with the new workflow (tag `v1.2.0` or later), so the
   Setup.exe and `SHA256SUMS.txt` are attached to it.
2. Generate and check the manifests:

   ```bash
   python packaging/winget/make_manifests.py 1.2.0
   winget validate packaging/winget/manifests/f/F0uP/StreetviewTourEditor/1.2.0
   winget install --manifest packaging/winget/manifests/f/F0uP/StreetviewTourEditor/1.2.0
   ```

3. Fork https://github.com/microsoft/winget-pkgs, copy the generated
   `manifests/f/F0uP/StreetviewTourEditor/1.2.0` folder into the same path
   there and open a pull request (or run `wingetcreate submit <folder>`).
   Microsoft's bots test the installer; a maintainer merges it, usually within
   a few days.

## Later versions (automatic)

Add a repository secret `WINGET_TOKEN`: a classic GitHub personal access
token with the `public_repo` scope. After each tagged release, the `winget`
job in `.github/workflows/build-release.yml` opens the update pull request
against winget-pkgs by itself.
