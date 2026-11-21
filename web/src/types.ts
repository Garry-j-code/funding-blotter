export interface Deal {
  company: string;
  company_key: string;
  description?: string;
  amount_usd: number | null;
  amount_raw?: string;
  stage?: string;
  location?: string;
  investors?: string;
  source?: string;
  url?: string;
  published_at: string;
  priority: number;
  score: number;
  sector_label?: string;
  sector_reason?: string;
}

export type FilterMode = "all" | "priority" | "disclosed" | "over5m";

export interface CareerJob {
  id: number;
  title: string;
  location?: string | null;
  url: string;
  role_type: string;
  posted_at?: string | null;
  first_seen: string;
  is_new: boolean;
}

/** One company's careers data. The last four fields are only sent to the admin. */
export interface Careers {
  company?: string;
  added_at?: string | null;         // YYYY-MM-DD: newest blotter date, or the day it was added on the Jobs page
  careers_url: string | null;
  ats: string | null;
  roles_tracked?: boolean;          // false: a saved page we cannot read roles from
  last_checked?: string | null;
  jobs: CareerJob[];
  verified?: boolean;
  status?: string;
  source?: string;
  consecutive_errors?: number;
  needs_refresh?: boolean;          // saved, waiting for the next run
  standalone?: boolean;             // added on the Jobs page; the company is not in the blotter
}

export type CareersMap = Record<string, Careers>;
