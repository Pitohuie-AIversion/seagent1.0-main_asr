## 2023-11-01 - [Regex Performance]
**Learning:** Python's `re` module caches a limited number of compiled expressions natively, but using `re.match` and `re.search` repeatedly inside loops on dynamic patterns (e.g. string formatting) or relying too heavily on uncompiled string literals can cause performance drops, especially when called frequently during extraction/parsing pipelines.
**Action:** When patterns are re-used, especially inside utility classes (like TemporalParser), explicitly `re.compile` them at the class/module level if possible.
