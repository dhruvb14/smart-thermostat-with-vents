import { useEffect, useRef, useState } from "react";
import { getHAEntities, type HAEntity } from "../api";

interface Props {
  domain: string;
  placeholder?: string;
  hasAttribute?: string;
  excludeIcon?: string;
  onSelect: (entityId: string) => void;
}

export default function EntityPicker({
  domain,
  placeholder,
  hasAttribute,
  excludeIcon,
  onSelect,
}: Props) {
  const [query, setQuery] = useState("");
  const [entities, setEntities] = useState<HAEntity[]>([]);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getHAEntities(domain, { hasAttribute, excludeIcon })
      .then(setEntities)
      .catch(() => {});
  }, [domain, hasAttribute, excludeIcon]);

  const filtered = entities
    .filter(
      (e) =>
        e.entity_id.toLowerCase().includes(query.toLowerCase()) ||
        e.friendly_name.toLowerCase().includes(query.toLowerCase())
    )
    .slice(0, 30);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div className="entity-picker" ref={ref}>
      <input
        className="form-control"
        placeholder={placeholder ?? `Search ${domain} entities…`}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
      />
      {open && filtered.length > 0 && (
        <div className="entity-dropdown">
          {filtered.map((e) => (
            <div
              key={e.entity_id}
              className="entity-option"
              onMouseDown={() => {
                onSelect(e.entity_id);
                setQuery("");
                setOpen(false);
              }}
            >
              <div>{e.friendly_name}</div>
              <div className="entity-id">{e.entity_id}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
