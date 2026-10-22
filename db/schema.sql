CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.profiles (
    id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    city text,
    region text,
    country_code text CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$'),
    postal_code text,
    latitude double precision CHECK (latitude IS NULL OR (latitude BETWEEN -90 AND 90)),
    longitude double precision CHECK (longitude IS NULL OR (longitude BETWEEN -180 AND 180)),
    timezone text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.subscription_tiers (
    id text PRIMARY KEY,
    display_name text NOT NULL,
    price_cents int NOT NULL CHECK (price_cents >= 0),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    interval text NOT NULL CHECK (interval IN ('month','year')),
    monthly_search_quota int NOT NULL CHECK (monthly_search_quota >= 0),
    saved_leads_quota int NOT NULL CHECK (saved_leads_quota >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.subscription_tiers
    (id, display_name, price_cents, currency, interval,
     monthly_search_quota, saved_leads_quota)
VALUES
    ('free', 'Free', 0,   'USD', 'month', 5,   20),
    ('pro',  'Pro',  499, 'USD', 'month', 100, 500)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.subscriptions (
    user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    tier_id text NOT NULL REFERENCES public.subscription_tiers(id),
    status text NOT NULL CHECK (status IN
        ('active','trialing','past_due','canceled','incomplete')),
    provider text,
    provider_customer_id text,
    provider_subscription_id text,
    current_period_start timestamptz,
    current_period_end timestamptz,
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.opportunity_leads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_hash char(64) NOT NULL UNIQUE
        CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    canonical_url text NOT NULL,
    title text NOT NULL,
    source_domain text,
    source_kind text CHECK (source_kind IN
        ('direct_program','directory','article','unknown')),
    description text,
    actual_start_date date,
    actual_end_date date,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.search_cache (
    cache_key text PRIMARY KEY,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS public.search_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    city text,
    zip_code text,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','complete','partial','failed')),
    started_at timestamptz,
    completed_at timestamptz,
    requests_attempted int NOT NULL DEFAULT 0,
    requests_completed int NOT NULL DEFAULT 0,
    requests_failed int NOT NULL DEFAULT 0,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    cache_hit boolean NOT NULL DEFAULT false,
    reservation_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS search_runs_user_idx
    ON public.search_runs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS search_runs_status_idx
    ON public.search_runs(status, created_at);

CREATE TABLE IF NOT EXISTS public.search_run_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES public.search_runs(id) ON DELETE CASCADE,
    lead_id uuid NOT NULL REFERENCES public.opportunity_leads(id) ON DELETE CASCADE,
    requested_city text,
    requested_zip text,
    snippet text,
    rank int NOT NULL,
    score int,
    kind text,
    eligibility_level text CHECK (eligibility_level IN
        ('explicit','ambiguous','unknown')),
    location_level text CHECK (location_level IN
        ('explicit','ambiguous','unknown')),
    volunteer_role text CHECK (volunteer_role IN
        ('youth_volunteers','youth_beneficiaries','unclear')),
    is_review_candidate boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS search_run_results_run_idx
    ON public.search_run_results(run_id, rank);
CREATE INDEX IF NOT EXISTS search_run_results_lead_idx
    ON public.search_run_results(lead_id);

CREATE TABLE IF NOT EXISTS public.lead_evidence (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id uuid NOT NULL REFERENCES public.opportunity_leads(id) ON DELETE CASCADE,
    run_id uuid REFERENCES public.search_runs(id) ON DELETE SET NULL,
    source_url text NOT NULL,
    excerpt text,
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    classification_type text CHECK (classification_type IN
        ('eligibility','location','kind','volunteer_role')),
    classification_method text NOT NULL,
    confidence text CHECK (confidence IN ('explicit','ambiguous','unknown')),
    reasons jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS lead_evidence_lead_idx
    ON public.lead_evidence(lead_id);

CREATE TABLE IF NOT EXISTS public.saved_opportunities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    lead_id uuid NOT NULL REFERENCES public.opportunity_leads(id) ON DELETE CASCADE,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id, lead_id)
);

CREATE INDEX IF NOT EXISTS saved_opportunities_user_idx
    ON public.saved_opportunities(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.usage_counters (
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    period_start date NOT NULL,
    searches_committed int NOT NULL DEFAULT 0,
    searches_reserved int NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, period_start)
);

CREATE TABLE IF NOT EXISTS public.quota_reservations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    run_id uuid REFERENCES public.search_runs(id) ON DELETE SET NULL,
    period_start date NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','committed','refunded','stale')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL DEFAULT (now() + interval '10 minutes')
);

CREATE INDEX IF NOT EXISTS quota_reservations_pending_idx
    ON public.quota_reservations(status, expires_at);
CREATE INDEX IF NOT EXISTS quota_reservations_user_idx
    ON public.quota_reservations(user_id, period_start);

CREATE TABLE IF NOT EXISTS public.processed_billing_events (
    event_id text PRIMARY KEY,
    processed_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION public.tg_set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'profiles_set_updated_at') THEN
        CREATE TRIGGER profiles_set_updated_at BEFORE UPDATE ON public.profiles
        FOR EACH ROW EXECUTE FUNCTION public.tg_set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'subscriptions_set_updated_at') THEN
        CREATE TRIGGER subscriptions_set_updated_at BEFORE UPDATE ON public.subscriptions
        FOR EACH ROW EXECUTE FUNCTION public.tg_set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'opportunity_leads_set_updated_at') THEN
        CREATE TRIGGER opportunity_leads_set_updated_at BEFORE UPDATE ON public.opportunity_leads
        FOR EACH ROW EXECUTE FUNCTION public.tg_set_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'search_runs_set_updated_at') THEN
        CREATE TRIGGER search_runs_set_updated_at BEFORE UPDATE ON public.search_runs
        FOR EACH ROW EXECUTE FUNCTION public.tg_set_updated_at();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    INSERT INTO public.profiles (id) VALUES (NEW.id) ON CONFLICT DO NOTHING;
    INSERT INTO public.subscriptions (user_id, tier_id, status)
        VALUES (NEW.id, 'free', 'active')
        ON CONFLICT (user_id) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

ALTER TABLE public.profiles              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.opportunity_leads     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.search_runs           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.search_run_results    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lead_evidence         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.saved_opportunities   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subscription_tiers    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subscriptions         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.usage_counters        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.quota_reservations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.search_cache          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.processed_billing_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS profiles_select_own ON public.profiles;
CREATE POLICY profiles_select_own ON public.profiles FOR SELECT USING (id = auth.uid());
DROP POLICY IF EXISTS profiles_update_own ON public.profiles;
CREATE POLICY profiles_update_own ON public.profiles FOR UPDATE USING (id = auth.uid()) WITH CHECK (id = auth.uid());

DROP POLICY IF EXISTS leads_select_authenticated ON public.opportunity_leads;
CREATE POLICY leads_select_authenticated ON public.opportunity_leads FOR SELECT USING (auth.uid() IS NOT NULL);

DROP POLICY IF EXISTS search_runs_select_own ON public.search_runs;
CREATE POLICY search_runs_select_own ON public.search_runs FOR SELECT USING (user_id = auth.uid());

DROP POLICY IF EXISTS search_run_results_select_own ON public.search_run_results;
CREATE POLICY search_run_results_select_own ON public.search_run_results FOR SELECT USING (EXISTS (
    SELECT 1 FROM public.search_runs sr
    WHERE sr.id = search_run_results.run_id AND sr.user_id = auth.uid()
));

DROP POLICY IF EXISTS lead_evidence_select_own ON public.lead_evidence;
CREATE POLICY lead_evidence_select_own ON public.lead_evidence FOR SELECT USING (run_id IS NULL OR EXISTS (
    SELECT 1 FROM public.search_runs sr
    WHERE sr.id = lead_evidence.run_id AND sr.user_id = auth.uid()
));

DROP POLICY IF EXISTS saved_opportunities_select_own ON public.saved_opportunities;
CREATE POLICY saved_opportunities_select_own ON public.saved_opportunities FOR SELECT USING (user_id = auth.uid());
DROP POLICY IF EXISTS saved_opportunities_insert_own ON public.saved_opportunities;
CREATE POLICY saved_opportunities_insert_own ON public.saved_opportunities FOR INSERT WITH CHECK (user_id = auth.uid());
DROP POLICY IF EXISTS saved_opportunities_delete_own ON public.saved_opportunities;
CREATE POLICY saved_opportunities_delete_own ON public.saved_opportunities FOR DELETE USING (user_id = auth.uid());

DROP POLICY IF EXISTS subscription_tiers_select_all ON public.subscription_tiers;
CREATE POLICY subscription_tiers_select_all ON public.subscription_tiers FOR SELECT USING (true);

DROP POLICY IF EXISTS subscriptions_select_own ON public.subscriptions;
CREATE POLICY subscriptions_select_own ON public.subscriptions FOR SELECT USING (user_id = auth.uid());

DROP POLICY IF EXISTS usage_counters_select_own ON public.usage_counters;
CREATE POLICY usage_counters_select_own ON public.usage_counters FOR SELECT USING (user_id = auth.uid());

DROP POLICY IF EXISTS quota_reservations_select_own ON public.quota_reservations;
CREATE POLICY quota_reservations_select_own ON public.quota_reservations FOR SELECT USING (user_id = auth.uid());
