# Tool policy

Pre/post tool-use hooks read this file as guidance when classifying calls.
Override for your domain.

## Allowed without confirmation
- Read-only retrieval (`search_documents`, `read_document`)
- Read-only filesystem (`read_file`, `list_dir`)

## Requires confirmation in `default` permission mode
- Write/edit/delete tools
- Bash and shell-like tools
- Any tool marked `is_destructive`

## Always denied
- Tools touching paths outside the configured working directory.
