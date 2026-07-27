import { Link, useParams } from "react-router-dom";

// Placeholder shell — full detail view lands in task F4.
export default function Detail() {
  const { id } = useParams();
  return (
    <div className="container">
      <Link to="/">← back</Link>
      <h1>Call {id}</h1>
      <p>Detail view coming next.</p>
    </div>
  );
}
