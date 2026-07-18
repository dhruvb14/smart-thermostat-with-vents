import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import Alert from "./Alert";

describe("Alert", () => {
  it("renders the body with the variant class and alert role", () => {
    render(<Alert variant="warning">Vents need attention</Alert>);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Vents need attention");
    expect(alert).toHaveClass("alert", "alert-warning");
  });

  it.each(["info", "success", "danger"] as const)("applies the alert-%s class", (variant) => {
    render(<Alert variant={variant}>body</Alert>);
    expect(screen.getByRole("alert")).toHaveClass(`alert-${variant}`);
  });

  it("renders actions in a dedicated row when provided", () => {
    render(
      <Alert variant="info" actions={<button>Fix it</button>}>
        body
      </Alert>
    );
    const button = screen.getByRole("button", { name: "Fix it" });
    expect(button.parentElement).toHaveClass("alert-actions");
  });

  it("omits the actions row when no actions are given", () => {
    const { container } = render(<Alert variant="info">body</Alert>);
    expect(container.querySelector(".alert-actions")).toBeNull();
  });

  it("forwards testId and appends className without clobbering base classes", () => {
    render(
      <Alert variant="danger" testId="my-alert" className="mt-2">
        body
      </Alert>
    );
    const alert = screen.getByTestId("my-alert");
    expect(alert).toHaveClass("alert", "alert-danger", "mt-2");
  });
});
