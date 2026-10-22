# Signing the Windows build with SignPath

Unsigned, the Setup.exe makes Windows SmartScreen warn ("Windows protected your PC")
until enough people have run it. [SignPath Foundation](https://signpath.org) signs
open-source projects for free; the release workflow uses it when it is set up.
(Azure Trusted Signing works too, see the top of `.github/workflows/build-release.yml`;
if both are set up, Azure is used.)

## Once

1. Apply at <https://signpath.org/apply> with this repository. SignPath checks the
   project and creates an organization and a project for it.
2. In SignPath, add two **artifact configurations** to the project, with these slugs
   and contents (GitHub hands every file over as a zip):

   `app` - the program itself:

   ```xml
   <?xml version="1.0" encoding="utf-8"?>
   <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
     <zip-file>
       <pe-file path="StreetviewTourEditor.exe">
         <authenticode-sign/>
       </pe-file>
     </zip-file>
   </artifact-configuration>
   ```

   `setup` - the installer:

   ```xml
   <?xml version="1.0" encoding="utf-8"?>
   <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
     <zip-file>
       <pe-file path="StreetviewTourEditor-*-Setup*.exe">
         <authenticode-sign/>
       </pe-file>
     </zip-file>
   </artifact-configuration>
   ```

3. Link SignPath to GitHub as a trusted build system (SignPath's docs: "Trusted build
   systems → GitHub") so it accepts files built by this repository's workflow.
4. In the GitHub repository, under *Settings → Secrets and variables → Actions*:
   - secret `SIGNPATH_API_TOKEN`: an API token of a SignPath CI user that may submit
     signing requests for the project,
   - variables `SIGNPATH_ORGANIZATION_ID` and `SIGNPATH_PROJECT_SLUG`,
   - variable `SIGNPATH_SIGNING_POLICY_SLUG` if the policy is not called `release-signing`.

## What the workflow does

With the secret and variables set, each Windows build runs in two steps
(`build_exe.py --stage app`, then `--stage package`):

1. builds the app folder and has `StreetviewTourEditor.exe` signed,
2. builds the portable zip and the installer around the signed program and has the
   `Setup.exe` signed.

Without them, the builds stay unsigned and everything else works as before. The
uninstaller inside the Setup.exe is only signed with Azure Trusted Signing (Inno Setup
signs it while it builds the installer).
