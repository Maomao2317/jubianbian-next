# Release workflow

For every change, complete code review and the relevant checks first, then
commit and push the verified source to Git before deploying it. Deploy the
same pushed revision to staging, verify it, then deploy to production.
Record the pushed commit and deployment verification. Preserve unrelated
working-tree changes and do not include credentials or business data in Git.
