/**
 * One header for every page: title, one-line purpose, right-aligned actions.
 *
 * The description is not decoration. Three of these pages (Matrix, Router,
 * Suites) are only distinguishable from each other by what question they
 * answer, so each page states its question in the header rather than
 * leaving the user to infer it from the table below.
 */
export default function PageHeader({ title, description, meta, children }) {
  return (
    <div className="page-header">
      <div className="page-header-text">
        <h1 className="page-title">{title}</h1>
        {description && <p className="page-desc">{description}</p>}
      </div>
      {(meta || children) && (
        <div className="page-header-actions">
          {meta && <span className="page-header-meta">{meta}</span>}
          {children}
        </div>
      )}
    </div>
  )
}
