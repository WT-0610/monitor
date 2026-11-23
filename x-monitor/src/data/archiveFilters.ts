export interface ArchiveFilters {
  uid: string;
  kw: string;
  media: string;
  sort: string;
  desc: boolean;
  start: string;
  end: string;
  kind: string;
}

export const emptyArchiveFilters: ArchiveFilters = {
  uid: "", kw: "", media: "", sort: "created_ts", desc: true,
  start: "", end: "", kind: "",
};
