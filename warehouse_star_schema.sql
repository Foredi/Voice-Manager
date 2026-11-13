-- VoiceMaster warehouse star schema.
-- Safe to re-run. Existing VoiceMaster configuration and voice history are retained.

create table if not exists public.guild_settings (
    guild_id bigint primary key,
    category_id bigint not null,
    trigger_channel_id bigint not null,
    created_at timestamptz not null default now()
);

create table if not exists public.rooms (
    channel_id bigint primary key,
    guild_id bigint not null,
    owner_id bigint not null,
    created_at timestamptz not null default now()
);
create index if not exists idx_rooms_guild on public.rooms (guild_id);

create table if not exists public.voice_presence (
    guild_id bigint primary key,
    channel_id bigint not null
);

create table if not exists public.polls (
    id uuid primary key,
    guild_id bigint not null,
    channel_id bigint not null,
    message_id bigint,
    creator_id bigint not null,
    creator_name text not null,
    question text not null,
    description text,
    embed_title text,
    embed_color integer not null default 14198783,
    thumbnail_url text,
    image_url text,
    author_name text,
    author_icon_url text,
    footer_text text,
    footer_icon_url text,
    single_choice boolean not null default true,
    ends_at timestamptz,
    ended boolean not null default false,
    ended_at timestamptz,
    created_at timestamptz not null default now()
);
alter table public.polls add column if not exists embed_title text;
alter table public.polls add column if not exists embed_color integer not null default 14198783;
alter table public.polls add column if not exists thumbnail_url text;
alter table public.polls add column if not exists image_url text;
alter table public.polls add column if not exists author_name text;
alter table public.polls add column if not exists author_icon_url text;
alter table public.polls add column if not exists footer_text text;
alter table public.polls add column if not exists footer_icon_url text;
create table if not exists public.poll_embed_templates (
    id uuid primary key default gen_random_uuid(),
    guild_id bigint not null,
    owner_id bigint not null,
    name text not null,
    embed_title text,
    description text,
    embed_color integer not null default 14198783,
    thumbnail_url text,
    image_url text,
    author_name text,
    author_icon_url text,
    footer_text text,
    footer_icon_url text,
    created_at timestamptz not null default now(),
    unique (guild_id, name)
);
alter table public.poll_embed_templates add column if not exists author_name text;
alter table public.poll_embed_templates add column if not exists author_icon_url text;
alter table public.poll_embed_templates add column if not exists footer_text text;
alter table public.poll_embed_templates add column if not exists footer_icon_url text;
create index if not exists idx_poll_templates_guild on public.poll_embed_templates(guild_id, created_at desc);
create index if not exists idx_polls_active_expiry on public.polls(ends_at) where ended = false;
create index if not exists idx_polls_channel_active on public.polls(guild_id, channel_id, created_at desc) where ended = false;

create table if not exists public.poll_options (
    id bigint generated always as identity primary key,
    poll_id uuid not null references public.polls(id) on delete cascade,
    position integer not null,
    label text not null,
    emoji text,
    unique(poll_id, position)
);
create table if not exists public.poll_votes (
    poll_id uuid not null references public.polls(id) on delete cascade,
    user_id bigint not null,
    option_id bigint not null references public.poll_options(id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key(poll_id, user_id, option_id)
);
create index if not exists idx_poll_votes_option on public.poll_votes(poll_id, option_id);

-- A vote replacement is one transaction and validates poll state and option ownership.
create or replace function public.cast_poll_vote(
    p_poll_id uuid,
    p_user_id bigint,
    p_option_ids bigint[]
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    poll_record public.polls%rowtype;
begin
    select * into poll_record from public.polls where id = p_poll_id for update;
    if not found or poll_record.ended or (poll_record.ends_at is not null and poll_record.ends_at <= now()) then
        raise exception 'poll is closed';
    end if;
    if poll_record.single_choice and coalesce(array_length(p_option_ids, 1), 0) > 1 then
        raise exception 'poll accepts one option';
    end if;
    if exists (
        select 1 from unnest(coalesce(p_option_ids, '{}'::bigint[])) requested(id)
        left join public.poll_options option_row on option_row.id = requested.id and option_row.poll_id = p_poll_id
        where option_row.id is null
    ) then
        raise exception 'invalid option';
    end if;
    delete from public.poll_votes where poll_id = p_poll_id and user_id = p_user_id;
    insert into public.poll_votes(poll_id, user_id, option_id)
    select p_poll_id, p_user_id, selected.id
    from (select distinct unnest(coalesce(p_option_ids, '{}'::bigint[])) as id) selected;
end;
$$;

create table if not exists public.room_name_preferences (
    guild_id bigint not null,
    user_id bigint not null,
    room_name text not null,
    primary key (guild_id, user_id)
);

-- Dimensions keep durable labels/identity after Discord members or channels leave.
create table if not exists public.dim_guild (
    guild_id bigint primary key,
    guild_name text,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    departed_at timestamptz
);

create table if not exists public.dim_member (
    guild_id bigint not null references public.dim_guild(guild_id),
    user_id bigint not null,
    member_name text,
    is_bot boolean not null default false,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    departed_at timestamptz,
    primary key (guild_id, user_id)
);

create table if not exists public.dim_voice_channel (
    channel_id bigint primary key,
    guild_id bigint not null references public.dim_guild(guild_id),
    channel_name text,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    deleted_at timestamptz
);
create index if not exists idx_dim_voice_channel_guild on public.dim_voice_channel(guild_id);

-- A fact row is opened on join and closed on leave/move. Open rows survive restarts.
create table if not exists public.fact_voice_session (
    session_id uuid primary key default gen_random_uuid(),
    source_session_id bigint unique,
    guild_id bigint not null references public.dim_guild(guild_id),
    user_id bigint not null,
    channel_id bigint,
    started_at timestamptz not null,
    ended_at timestamptz,
    last_seen_at timestamptz not null default now(),
    duration_seconds bigint check (duration_seconds is null or duration_seconds >= 0),
    close_reason text,
    foreign key (guild_id, user_id) references public.dim_member(guild_id, user_id),
    foreign key (channel_id) references public.dim_voice_channel(channel_id),
    check (ended_at is null or ended_at >= started_at)
);
create index if not exists idx_fact_voice_member_time
    on public.fact_voice_session(guild_id, user_id, started_at desc);
create index if not exists idx_fact_voice_room_time
    on public.fact_voice_session(guild_id, channel_id, started_at desc);
create unique index if not exists idx_fact_voice_one_active_session
    on public.fact_voice_session(guild_id, user_id) where ended_at is null;

-- Legacy aggregates have no timestamps or room attribution. Keep their remainder
-- separate so lifetime totals remain accurate without inventing historical detail.
create table if not exists public.fact_voice_legacy_total (
    guild_id bigint not null references public.dim_guild(guild_id),
    user_id bigint not null,
    total_seconds bigint not null check (total_seconds > 0),
    source text not null default 'legacy_voice_time',
    imported_at timestamptz not null default now(),
    primary key (guild_id, user_id),
    foreign key (guild_id, user_id) references public.dim_member(guild_id, user_id)
);

-- Backfill dimensions first from the existing app tables when they are present.
do $$
begin
    if to_regclass('public.voice_time') is not null then
        execute $q$
            insert into public.dim_guild(guild_id)
            select distinct guild_id from public.voice_time
            on conflict (guild_id) do nothing
        $q$;
        execute $q$
            insert into public.dim_member(guild_id, user_id)
            select guild_id, user_id from public.voice_time
            on conflict (guild_id, user_id) do nothing
        $q$;
    end if;
    if to_regclass('public.rooms') is not null then
        execute $q$
            insert into public.dim_guild(guild_id)
            select distinct guild_id from public.rooms
            on conflict (guild_id) do nothing
        $q$;
        execute $q$
            insert into public.dim_member(guild_id, user_id)
            select distinct guild_id, owner_id from public.rooms
            on conflict (guild_id, user_id) do nothing
        $q$;
        execute $q$
            insert into public.dim_voice_channel(channel_id, guild_id)
            select channel_id, guild_id from public.rooms
            on conflict (channel_id) do nothing
        $q$;
    end if;
    if to_regclass('public.voice_time_sessions') is not null then
        execute $q$
            insert into public.dim_guild(guild_id)
            select distinct guild_id from public.voice_time_sessions
            on conflict (guild_id) do nothing
        $q$;
        execute $q$
            insert into public.dim_member(guild_id, user_id)
            select distinct guild_id, user_id from public.voice_time_sessions
            on conflict (guild_id, user_id) do nothing
        $q$;
        execute $q$
            insert into public.dim_voice_channel(channel_id, guild_id)
            select distinct (to_jsonb(s)->>'channel_id')::bigint, s.guild_id
            from public.voice_time_sessions s
            where to_jsonb(s)->>'channel_id' is not null
            on conflict (channel_id) do nothing
        $q$;
        execute $q$
            insert into public.fact_voice_session(
                source_session_id, guild_id, user_id, channel_id,
                started_at, ended_at, last_seen_at, duration_seconds, close_reason
            )
            select s.id,
                   s.guild_id,
                   s.user_id,
                   nullif(to_jsonb(s)->>'channel_id', '')::bigint,
                   s.started_at,
                   s.ended_at,
                   s.ended_at,
                   s.duration_seconds,
                   'legacy_import'
            from public.voice_time_sessions s
            where s.ended_at is not null
            on conflict (source_session_id) do nothing
        $q$;
    end if;
end $$;

-- Import only the aggregate remainder not represented by the old session rows.
do $$
begin
    if to_regclass('public.voice_time') is not null then
        if to_regclass('public.voice_time_sessions') is not null then
            execute $q$
                insert into public.fact_voice_legacy_total(guild_id, user_id, total_seconds)
                select old_total.guild_id,
                       old_total.user_id,
                       greatest(old_total.total_seconds - coalesce(sum(old_session.duration_seconds), 0), 0)
                from public.voice_time old_total
                left join public.voice_time_sessions old_session
                  on old_session.guild_id = old_total.guild_id
                 and old_session.user_id = old_total.user_id
                group by old_total.guild_id, old_total.user_id, old_total.total_seconds
                having greatest(old_total.total_seconds - coalesce(sum(old_session.duration_seconds), 0), 0) > 0
                on conflict (guild_id, user_id) do update
                set total_seconds = excluded.total_seconds,
                    imported_at = now()
            $q$;
        else
            execute $q$
                insert into public.fact_voice_legacy_total(guild_id, user_id, total_seconds)
                select guild_id, user_id, total_seconds from public.voice_time where total_seconds > 0
                on conflict (guild_id, user_id) do update
                set total_seconds = excluded.total_seconds,
                    imported_at = now()
            $q$;
        end if;
    end if;
end $$;

create or replace function public.start_voice_session(
    p_guild_id bigint,
    p_guild_name text,
    p_user_id bigint,
    p_member_name text,
    p_is_bot boolean,
    p_channel_id bigint,
    p_channel_name text,
    p_started_at timestamptz default now()
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    current_session public.fact_voice_session%rowtype;
    new_session_id uuid;
begin
    perform pg_advisory_xact_lock(hashtextextended(p_guild_id::text || ':' || p_user_id::text, 0));

    insert into public.dim_guild(guild_id, guild_name, last_seen_at, departed_at)
    values (p_guild_id, p_guild_name, p_started_at, null)
    on conflict (guild_id) do update
      set guild_name = excluded.guild_name, last_seen_at = excluded.last_seen_at, departed_at = null;
    insert into public.dim_member(guild_id, user_id, member_name, is_bot, last_seen_at, departed_at)
    values (p_guild_id, p_user_id, p_member_name, coalesce(p_is_bot, false), p_started_at, null)
    on conflict (guild_id, user_id) do update
      set member_name = excluded.member_name, is_bot = excluded.is_bot,
          last_seen_at = excluded.last_seen_at, departed_at = null;
    insert into public.dim_voice_channel(channel_id, guild_id, channel_name, last_seen_at, deleted_at)
    values (p_channel_id, p_guild_id, p_channel_name, p_started_at, null)
    on conflict (channel_id) do update
      set channel_name = excluded.channel_name, last_seen_at = excluded.last_seen_at, deleted_at = null;

    select * into current_session
    from public.fact_voice_session
    where guild_id = p_guild_id and user_id = p_user_id and ended_at is null
    for update;
    if found and current_session.channel_id = p_channel_id then
        update public.fact_voice_session
           set last_seen_at = greatest(last_seen_at, p_started_at)
         where session_id = current_session.session_id;
        return current_session.session_id;
    end if;
    if found then
        update public.fact_voice_session
           set ended_at = greatest(p_started_at, started_at),
               last_seen_at = greatest(last_seen_at, p_started_at),
               duration_seconds = greatest(0, floor(extract(epoch from (greatest(p_started_at, started_at) - started_at)))::bigint),
               close_reason = 'channel_change'
         where session_id = current_session.session_id;
    end if;

    insert into public.fact_voice_session(guild_id, user_id, channel_id, started_at, last_seen_at)
    values (p_guild_id, p_user_id, p_channel_id, p_started_at, p_started_at)
    returning session_id into new_session_id;
    return new_session_id;
end;
$$;

create or replace function public.close_voice_session(
    p_guild_id bigint,
    p_user_id bigint,
    p_ended_at timestamptz default now(),
    p_reason text default 'voice_leave'
)
returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare closed_seconds bigint;
begin
    perform pg_advisory_xact_lock(hashtextextended(p_guild_id::text || ':' || p_user_id::text, 0));
    update public.fact_voice_session
       set ended_at = greatest(p_ended_at, started_at),
           last_seen_at = greatest(last_seen_at, least(p_ended_at, now())),
           duration_seconds = greatest(0, floor(extract(epoch from (greatest(p_ended_at, started_at) - started_at)))::bigint),
           close_reason = p_reason
     where guild_id = p_guild_id and user_id = p_user_id and ended_at is null
     returning duration_seconds into closed_seconds;
    update public.dim_member set last_seen_at = greatest(last_seen_at, least(p_ended_at, now()))
     where guild_id = p_guild_id and user_id = p_user_id;
    return coalesce(closed_seconds, 0);
end;
$$;

create or replace function public.close_channel_voice_sessions(p_channel_id bigint, p_ended_at timestamptz default now())
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare closed_count integer;
begin
    update public.fact_voice_session
       set ended_at = greatest(p_ended_at, started_at),
           last_seen_at = greatest(last_seen_at, least(p_ended_at, now())),
           duration_seconds = greatest(0, floor(extract(epoch from (greatest(p_ended_at, started_at) - started_at)))::bigint),
           close_reason = 'channel_deleted'
     where channel_id = p_channel_id and ended_at is null;
    get diagnostics closed_count = row_count;
    update public.dim_voice_channel set deleted_at = p_ended_at, last_seen_at = greatest(last_seen_at, p_ended_at)
     where channel_id = p_channel_id;
    return closed_count;
end;
$$;

create or replace function public.touch_voice_sessions(p_guild_id bigint, p_user_ids bigint[], p_seen_at timestamptz default now())
returns integer
language sql
security definer
set search_path = pg_catalog, public
as $$
    with touched as (
        update public.fact_voice_session
           set last_seen_at = greatest(last_seen_at, p_seen_at)
         where guild_id = p_guild_id and user_id = any(p_user_ids) and ended_at is null
        returning user_id
    )
    select count(*)::integer from touched;
$$;

create or replace function public.get_voice_total(p_guild_id bigint, p_user_id bigint)
returns bigint
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
    select coalesce((select total_seconds from public.fact_voice_legacy_total where guild_id = p_guild_id and user_id = p_user_id), 0)
         + coalesce((select sum(coalesce(duration_seconds, floor(extract(epoch from (now() - started_at)))::bigint))
                     from public.fact_voice_session where guild_id = p_guild_id and user_id = p_user_id), 0)::bigint;
$$;

drop function if exists public.get_voice_companion_leaderboard(bigint, bigint, integer);
create function public.get_voice_companion_leaderboard(p_guild_id bigint, p_user_id bigint, p_limit integer default 5)
returns table(user_id bigint, member_name text, total_seconds bigint)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
    select other_session.user_id,
           coalesce(member_dimension.member_name, 'Unknown member') as member_name,
           sum(extract(epoch from (
               least(coalesce(my_session.ended_at, now()), coalesce(other_session.ended_at, now()), now())
               - greatest(my_session.started_at, other_session.started_at)
           )))::bigint as total_seconds
    from public.fact_voice_session my_session
    join public.fact_voice_session other_session
      on other_session.guild_id = my_session.guild_id
     and other_session.channel_id = my_session.channel_id
     and other_session.user_id <> p_user_id
     and other_session.started_at < coalesce(my_session.ended_at, now())
     and coalesce(other_session.ended_at, now()) > my_session.started_at
    left join public.dim_member member_dimension
      on member_dimension.guild_id = other_session.guild_id
     and member_dimension.user_id = other_session.user_id
    where my_session.guild_id = p_guild_id
      and my_session.user_id = p_user_id
      and my_session.channel_id is not null
      and least(coalesce(my_session.ended_at, now()), coalesce(other_session.ended_at, now()), now())
          > greatest(my_session.started_at, other_session.started_at)
    group by other_session.user_id, member_dimension.member_name
    order by total_seconds desc
    limit greatest(1, least(coalesce(p_limit, 5), 20));
$$;

create or replace function public.mark_member_departed(p_guild_id bigint, p_user_id bigint, p_departed_at timestamptz default now())
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
    perform public.close_voice_session(p_guild_id, p_user_id, p_departed_at, 'member_departed');
    update public.dim_member set departed_at = p_departed_at, last_seen_at = greatest(last_seen_at, p_departed_at)
     where guild_id = p_guild_id and user_id = p_user_id;
end;
$$;

alter table public.guild_settings enable row level security;
alter table public.rooms enable row level security;
alter table public.voice_presence enable row level security;
alter table public.room_name_preferences enable row level security;
alter table public.dim_guild enable row level security;
alter table public.dim_member enable row level security;
alter table public.dim_voice_channel enable row level security;
alter table public.fact_voice_session enable row level security;
alter table public.fact_voice_legacy_total enable row level security;
grant select, insert, update, delete on public.dim_guild, public.dim_member, public.dim_voice_channel,
    public.fact_voice_session, public.fact_voice_legacy_total to service_role;

revoke all on function public.start_voice_session(bigint, text, bigint, text, boolean, bigint, text, timestamptz) from public, anon, authenticated;
revoke all on function public.close_voice_session(bigint, bigint, timestamptz, text) from public, anon, authenticated;
revoke all on function public.close_channel_voice_sessions(bigint, timestamptz) from public, anon, authenticated;
revoke all on function public.touch_voice_sessions(bigint, bigint[], timestamptz) from public, anon, authenticated;
revoke all on function public.get_voice_total(bigint, bigint) from public, anon, authenticated;
revoke all on function public.get_voice_companion_leaderboard(bigint, bigint, integer) from public, anon, authenticated;
revoke all on function public.mark_member_departed(bigint, bigint, timestamptz) from public, anon, authenticated;
grant execute on function public.start_voice_session(bigint, text, bigint, text, boolean, bigint, text, timestamptz) to service_role;
grant execute on function public.close_voice_session(bigint, bigint, timestamptz, text) to service_role;
grant execute on function public.close_channel_voice_sessions(bigint, timestamptz) to service_role;
grant execute on function public.touch_voice_sessions(bigint, bigint[], timestamptz) to service_role;
grant execute on function public.get_voice_total(bigint, bigint) to service_role;
grant execute on function public.get_voice_companion_leaderboard(bigint, bigint, integer) to service_role;
grant execute on function public.mark_member_departed(bigint, bigint, timestamptz) to service_role;

notify pgrst, 'reload schema';
