package runtime

import (
	"encoding/json"
	"testing"
)

func TestRecreateKeepsUserSettingsAndDropsTheImagesOwn(t *testing.T) {
	var raw, imageCfg map[string]any
	_ = json.Unmarshal([]byte(`{
	  "Id": "abc123def456",
	  "Config": {
	    "Hostname": "abc123def456", "Image": "web:2",
	    "Cmd": ["sh", "-c", "exit 3"], "Entrypoint": null, "WorkingDir": "/app",
	    "Env": ["PATH=/usr/bin", "DATABASE_URL=postgres://db/app"],
	    "Labels": {"com.docker.compose.project": "shop", "org.opencontainers.image.version": "2"},
	    "ExposedPorts": {"80/tcp": {}, "9000/tcp": {}}
	  },
	  "HostConfig": {"Binds": ["/srv/conf:/etc/app:ro"], "Mounts": [{"Type": "volume", "Source": "db", "Target": "/var/lib/db"}], "RestartPolicy": {"Name": "always"}},
	  "Mounts": [
	    {"Type": "bind", "Source": "/srv/conf", "Destination": "/etc/app", "RW": false},
	    {"Type": "volume", "Name": "db", "Destination": "/var/lib/db", "RW": true},
	    {"Type": "volume", "Name": "0f3c9a", "Destination": "/cache", "RW": true}
	  ],
	  "NetworkSettings": {"Networks": {"shop_default": {"Aliases": ["web"], "IPAddress": "172.18.0.4", "EndpointID": "x"}}}
	}`), &raw)
	_ = json.Unmarshal([]byte(`{
	  "Cmd": ["sh", "-c", "exit 3"], "WorkingDir": "/srv",
	  "Env": ["PATH=/usr/bin"],
	  "Labels": {"org.opencontainers.image.version": "2"},
	  "ExposedPorts": {"80/tcp": {}}
	}`), &imageCfg)

	body, err := recreateBody(raw, imageCfg, "sha256:old")
	if err != nil {
		t.Fatal(err)
	}
	if body["Image"] != "sha256:old" {
		t.Fatalf("image %v", body["Image"])
	}
	if _, ok := body["Cmd"]; ok {
		t.Error("the bad image's command must not be copied")
	}
	if _, ok := body["Hostname"]; ok {
		t.Error("the default hostname (the old container id) must not be copied")
	}
	if body["WorkingDir"] != "/app" {
		t.Error("a working dir the user set is kept")
	}
	if env, _ := json.Marshal(body["Env"]); string(env) != `["DATABASE_URL=postgres://db/app"]` {
		t.Errorf("env %s", env)
	}
	if labels, _ := json.Marshal(body["Labels"]); string(labels) != `{"com.docker.compose.project":"shop"}` {
		t.Errorf("labels %s", labels)
	}
	if ports, _ := json.Marshal(body["ExposedPorts"]); string(ports) != `{"9000/tcp":{}}` {
		t.Errorf("ports %s", ports)
	}
	host := body["HostConfig"].(map[string]any)
	mounts, _ := json.Marshal(host["Mounts"])
	want := `[{"Source":"db","Target":"/var/lib/db","Type":"volume"},{"ReadOnly":false,"Source":"0f3c9a","Target":"/cache","Type":"volume"}]`
	if string(mounts) != want {
		t.Errorf("mounts %s", mounts)
	}
	nets, _ := json.Marshal(body["NetworkingConfig"])
	if string(nets) != `{"EndpointsConfig":{"shop_default":{"Aliases":["web"]}}}` {
		t.Errorf("networks %s", nets)
	}
}
