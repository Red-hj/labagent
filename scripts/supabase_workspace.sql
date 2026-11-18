-- Run once in the existing Supabase project's SQL Editor.
-- Only the server's service-role key can access this metadata.
BEGIN;
CREATE TABLE IF NOT EXISTS public.labagent_workspace_head (
    id text PRIMARY KEY CHECK (id = 'main'),
    revision text NOT NULL,
    manifest jsonb NOT NULL,
    previous jsonb NOT NULL DEFAULT '[]'::jsonb
);
ALTER TABLE public.labagent_workspace_head ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.labagent_workspace_head FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.labagent_workspace_head TO service_role;
COMMIT;
NOTIFY pgrst, 'reload schema';
