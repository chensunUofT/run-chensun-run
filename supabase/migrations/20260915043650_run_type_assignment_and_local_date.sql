-- Keep user-selected training types stable across repeat imports.
ALTER TABLE public.runs
    ADD COLUMN IF NOT EXISTS run_type_assignment VARCHAR(20) NOT NULL DEFAULT 'unassigned';
ALTER TABLE public.runs
    ADD COLUMN IF NOT EXISTS source_utc_offset_seconds INTEGER;
