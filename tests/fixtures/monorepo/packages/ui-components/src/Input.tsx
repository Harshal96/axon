import React from "react";

export function Input({ value }: { value: string }) {
  return <input value={value} readOnly />;
}
