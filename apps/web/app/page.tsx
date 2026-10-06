import { HealthStatus } from "@/components/health-status";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default function Home() {
  return (
    <main className="flex min-h-screen items-center justify-center p-8">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-2xl">Groundcrew</CardTitle>
          <CardDescription>
            An AI DevOps team built on NVIDIA Nemotron models.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HealthStatus />
        </CardContent>
      </Card>
    </main>
  );
}
