/**
 * Links into the customer's AWS console. A connection knows its region once discovery has run;
 * before that the console opens in the last region the person used, and ARNs carry a REGION
 * placeholder to replace.
 */

export const REGION_PLACEHOLDER = "REGION";

export function cloudFormationStacksUrl(region: string | null | undefined): string {
  return region
    ? `https://${region}.console.aws.amazon.com/cloudformation/home?region=${region}#/stacks`
    : "https://console.aws.amazon.com/cloudformation/home#/stacks";
}
