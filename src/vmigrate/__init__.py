"""vmigrate — self-hosted, pluggable any-to-any VM migration framework.

Purpose: package root. Layout:
  ir/         normalized intermediate VM model (VmSpec) every provider maps into/out of
  core/       provider ABCs, refs, run context (dry-run guard), errors, byte-range math
  engine/     provider-agnostic pipeline: safety gates, copy, state/resume, verify, report
  config/     YAML loading + validation + secret interpolation
  osmorph/    (guest OS family x target platform) strategy registry
  providers/  one isolated adapter package per platform
  cli/        `vmigrate` command line
"""

__version__ = "0.1.0"
