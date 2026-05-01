import { create } from "zustand";

interface UIState {
  selectedEntityId?: string;
  selectedEventType: string;
  entityTypeFilter: string;
  entityIdFilter: string;
  searchQuery: string;
  followSelectedEntity: boolean;
  setSelectedEntityId: (entityId?: string) => void;
  setSelectedEventType: (eventType: string) => void;
  setEntityTypeFilter: (entityType: string) => void;
  setEntityIdFilter: (entityId: string) => void;
  setSearchQuery: (query: string) => void;
  setFollowSelectedEntity: (enabled: boolean) => void;
  resetFilters: () => void;
}

export const useUIStore = create<UIState>((set) => ({
  selectedEntityId: undefined,
  selectedEventType: "",
  entityTypeFilter: "",
  entityIdFilter: "",
  searchQuery: "",
  followSelectedEntity: true,
  setSelectedEntityId: (selectedEntityId) => set({ selectedEntityId }),
  setSelectedEventType: (selectedEventType) => set({ selectedEventType }),
  setEntityTypeFilter: (entityTypeFilter) => set({ entityTypeFilter }),
  setEntityIdFilter: (entityIdFilter) => set({ entityIdFilter }),
  setSearchQuery: (searchQuery) => set({ searchQuery }),
  setFollowSelectedEntity: (followSelectedEntity) => set({ followSelectedEntity }),
  resetFilters: () =>
    set({
      selectedEventType: "",
      entityTypeFilter: "",
      entityIdFilter: "",
      searchQuery: "",
      followSelectedEntity: true,
    }),
}));
