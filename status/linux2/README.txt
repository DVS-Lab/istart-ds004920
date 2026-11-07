Linux2 processing handoff

Status and sanitized log tails are committed directly to main in this directory.
status.json records resource, preprocessing and L1 state. log-tails.txt contains
tails of the explicit preprocessing log allowlist. Source code, cached work and
imaging derivatives are not modified by status publication.

The initial snapshot here was preserved from linux2-status before deleting that
branch. Its commit history is retained as a parent of the migration commit.
Status timestamps are UTC. Visual QC is separate and does not block preprocessing.
