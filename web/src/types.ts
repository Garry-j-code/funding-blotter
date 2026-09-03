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

export type FilterMode = "all" | "priority" | "early" | "disclosed" | "over5m";
