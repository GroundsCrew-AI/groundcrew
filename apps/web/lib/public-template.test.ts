import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// https://groundkrew.com/aws-connect.yaml is apps/web/public/aws-connect.yaml. Users download it and
// upload it to CloudFormation, so it must be exactly the template in infra/.
// The Kubernetes RBAC files are served the same way for the connect docs.
describe("the served connect files", () => {
  it.each(["aws-connect.yaml", "k8s-readonly-rbac.yaml", "k8s-operate-rbac.yaml"])(
    "%s is the repository's infra/ file",
    (name) => {
      const served = readFileSync(path.resolve(__dirname, "../public", name), "utf8");
      const source = readFileSync(path.resolve(__dirname, "../../../infra", name), "utf8");
      expect(served === source, `copy infra/${name} to apps/web/public/${name}`).toBe(true);
    },
  );
});
