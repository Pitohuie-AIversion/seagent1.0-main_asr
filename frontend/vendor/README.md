# Vendored frontend dependencies

These browser distributions are committed locally so the dialogue UI remains available offline.

| Package | Version | Source | Distributed file | SHA-256 |
| --- | --- | --- | --- | --- |
| Marked | 18.0.9 | https://www.npmjs.com/package/marked/v/18.0.9 | `marked/marked.umd.js` | `ba65f1c8948e6b01321399800843e9048b31e1c197652d4b0fafae840b30e32b` |
| DOMPurify | 3.4.13 | https://www.npmjs.com/package/dompurify/v/3.4.13 | `dompurify/purify.min.js` | `9ab3d44d73c3e3947f9ab72e0f0bc15c7f1931d60b365ba261fc85fe59013c56` |

Marked is provided under the MIT license; its license text is stored at `marked/LICENSE`.
DOMPurify is dual-licensed under Apache-2.0 or MPL-2.0; its license texts are stored at `dompurify/LICENSE` and `dompurify/LICENSE-MPL`.

When updating either dependency, replace the distribution and license files together, update the versions and hashes above, then run the frontend Markdown security tests and the complete project test suite.
