-- Race goals and owner-scoped plans. Existing records are not modified.


CREATE TABLE coaching_goals (
	id SERIAL NOT NULL, 
	owner_id UUID NOT NULL, 
	race_date DATE NOT NULL, 
	distance_km FLOAT NOT NULL, 
	target_seconds INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_coaching_goals_owner_id UNIQUE (owner_id)
)

;

CREATE INDEX ix_coaching_goals_owner_id ON coaching_goals (owner_id);

alter table public.coaching_goals enable row level security;
alter table public.coaching_goals force row level security;
create policy coaching_goals_owner_isolation on public.coaching_goals for all to runwise_api
using (owner_id::text = (select nullif(current_setting('request.jwt.claim.sub', true), '')))
with check (owner_id::text = (select nullif(current_setting('request.jwt.claim.sub', true), '')));
create trigger coaching_goals_owner_immutable before update on public.coaching_goals for each row execute function public.runwise_owner_immutable();
revoke all on public.coaching_goals from anon, authenticated;
revoke all on sequence public.coaching_goals_id_seq from anon, authenticated;
grant select, insert, update, delete on public.coaching_goals to runwise_api;
grant usage, select on sequence public.coaching_goals_id_seq to runwise_api;


CREATE TABLE coaching_schedules (
	id SERIAL NOT NULL, 
	owner_id UUID NOT NULL, 
	goal_id INTEGER NOT NULL, 
	weekday INTEGER NOT NULL, 
	run_type VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_coaching_schedule_owner_goal_weekday UNIQUE (owner_id, goal_id, weekday), 
	FOREIGN KEY(goal_id) REFERENCES coaching_goals (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_coaching_schedules_owner_goal ON coaching_schedules (owner_id, goal_id);

alter table public.coaching_schedules enable row level security;
alter table public.coaching_schedules force row level security;
create policy coaching_schedules_owner_isolation on public.coaching_schedules for all to runwise_api
using (owner_id::text = (select nullif(current_setting('request.jwt.claim.sub', true), '')))
with check (owner_id::text = (select nullif(current_setting('request.jwt.claim.sub', true), '')));
create trigger coaching_schedules_owner_immutable before update on public.coaching_schedules for each row execute function public.runwise_owner_immutable();
revoke all on public.coaching_schedules from anon, authenticated;
revoke all on sequence public.coaching_schedules_id_seq from anon, authenticated;
grant select, insert, update, delete on public.coaching_schedules to runwise_api;
grant usage, select on sequence public.coaching_schedules_id_seq to runwise_api;


CREATE TABLE coaching_sessions (
	id SERIAL NOT NULL, 
	owner_id UUID NOT NULL, 
	goal_id INTEGER NOT NULL, 
	date DATE NOT NULL, 
	run_type VARCHAR(20) NOT NULL, 
	distance_km FLOAT NOT NULL, 
	target_pace_seconds FLOAT NOT NULL, 
	description TEXT NOT NULL, 
	completed_run_id INTEGER, 
	manually_edited BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_coaching_sessions_owner_goal_date UNIQUE (owner_id, goal_id, date), 
	FOREIGN KEY(goal_id) REFERENCES coaching_goals (id) ON DELETE CASCADE, 
	FOREIGN KEY(completed_run_id) REFERENCES runs (id) ON DELETE SET NULL
)

;

CREATE INDEX ix_coaching_sessions_completed_run ON coaching_sessions (owner_id, completed_run_id);

CREATE INDEX ix_coaching_sessions_owner_goal_date ON coaching_sessions (owner_id, goal_id, date);

alter table public.coaching_sessions enable row level security;
alter table public.coaching_sessions force row level security;
create policy coaching_sessions_owner_isolation on public.coaching_sessions for all to runwise_api
using (owner_id::text = (select nullif(current_setting('request.jwt.claim.sub', true), '')))
with check (owner_id::text = (select nullif(current_setting('request.jwt.claim.sub', true), '')));
create trigger coaching_sessions_owner_immutable before update on public.coaching_sessions for each row execute function public.runwise_owner_immutable();
revoke all on public.coaching_sessions from anon, authenticated;
revoke all on sequence public.coaching_sessions_id_seq from anon, authenticated;
grant select, insert, update, delete on public.coaching_sessions to runwise_api;
grant usage, select on sequence public.coaching_sessions_id_seq to runwise_api;

create index ix_coaching_schedules_goal_id on public.coaching_schedules(goal_id);

create index ix_coaching_sessions_goal_id on public.coaching_sessions(goal_id);

create index ix_coaching_sessions_completed_run_id on public.coaching_sessions(completed_run_id);

-- Keep related rows in the same owner boundary, in addition to RLS.
alter table public.coaching_goals add constraint uq_coaching_goal_id_owner unique(id, owner_id);
alter table public.coaching_schedules add constraint fk_coaching_schedule_owner_goal foreign key(goal_id, owner_id) references public.coaching_goals(id, owner_id) on delete cascade;
alter table public.coaching_sessions add constraint fk_coaching_session_owner_goal foreign key(goal_id, owner_id) references public.coaching_goals(id, owner_id) on delete cascade;
alter table public.coaching_goals add constraint ck_coaching_goal_values check(distance_km > 0 and distance_km <= 1000 and target_seconds > 0 and target_seconds <= 604800);
alter table public.coaching_schedules add constraint ck_coaching_weekday check(weekday between 0 and 6 and run_type in ('easy','quality','long'));
alter table public.coaching_sessions add constraint ck_coaching_session_values check(distance_km >= 0 and distance_km <= 1000 and target_pace_seconds >= 0 and target_pace_seconds <= 86400 and run_type in ('easy','quality','tempo','interval','long','race','rest'));
