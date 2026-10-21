import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// https://groundkrew.com/aws-connect.yaml is apps/web/public/aws-connect.yaml. Users download it and
// upload it to CloudFormation, so it must be exactly the template in infra/.
describe("the served AWS connect template", () => {
  it("is the repository's infra/aws-connect.yaml", () => {
    const served = readFileSync(path.resolve(__dirname, "../public/aws-connect.yaml"), "utf8");
    const source = readFileSync(path.resolve(__dirname, "../../../infra/aws-connect.yaml"), "utf8");
    expect(served === source, "copy infra/aws-connect.yaml to apps/web/public/aws-connect.yaml").toBe(true);
  });
});
