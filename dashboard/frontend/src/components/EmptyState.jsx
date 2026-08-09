/**
 * Empty states that name the next step instead of just reporting absence.
 * Every dead end in the app should hand the user a button, not a sentence
 * telling them which tab to go find.
 */
export default function EmptyState({ title, description, actions = [] }) {
  return (
    <div className="empty">
      {title && <div className="empty-title">{title}</div>}
      {description && <div className="empty-desc">{description}</div>}
      {actions.length > 0 && (
        <div className="empty-actions">
          {actions.map((a) => (
            <button
              key={a.label}
              className={`btn ${a.primary ? 'primary' : ''}`}
              onClick={a.onClick}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
