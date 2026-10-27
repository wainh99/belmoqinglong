import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermission;
import java.util.*;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * Wispbyte / Pterodactyl-style Java panels - VLESS + WebSocket launcher.
 * Java port of free-dam-man/wispbyte-xray-ws-skill deploy/index.js.
 *
 * 1) downloads pinned Xray-core linux-64 zip (retryable)
 * 2) extracts `xray` with java.util.zip (stdlib, no system unzip)
 * 3) writes config from env and spawns xray on the panel port
 *
 * Env:
 *   SERVER_PORT | PORT   listen port (Pterodactyl injects SERVER_PORT)
 *   UUID                 VLESS id - set a fresh UUIDv4 per deploy
 *   WS_PATH              WebSocket path (default /tk-rail)
 *   XRAY_ZIP_URL         override release zip URL
 *   XRAY_LOGLEVEL        default warning
 */
public class Main {
    static final String TAG = "[wispbyte-ws]";

    static String env(String name, String def) {
        String v = System.getenv(name);
        return (v == null || v.isEmpty()) ? def : v;
    }

    static void die(String msg) {
        System.err.println(TAG + " FATAL: " + msg);
        System.exit(1);
    }

    static String buildConfig(int port, String uuid, String wsPath, String loglevel) {
        return "{\n"
            + "  \"log\": { \"loglevel\": \"" + loglevel + "\" },\n"
            + "  \"inbounds\": [{\n"
            + "    \"listen\": \"0.0.0.0\",\n"
            + "    \"port\": " + port + ",\n"
            + "    \"protocol\": \"vless\",\n"
            + "    \"settings\": {\n"
            + "      \"clients\": [{ \"id\": \"" + uuid + "\", \"email\": \"wispbyte-ws\" }],\n"
            + "      \"decryption\": \"none\"\n"
            + "    },\n"
            + "    \"streamSettings\": {\n"
            + "      \"network\": \"ws\",\n"
            + "      \"security\": \"none\",\n"
            + "      \"wsSettings\": { \"path\": \"" + wsPath + "\" }\n"
            + "    },\n"
            + "    \"sniffing\": {\n"
            + "      \"enabled\": true,\n"
            + "      \"destOverride\": [\"http\", \"tls\", \"quic\"]\n"
            + "    }\n"
            + "  }],\n"
            + "  \"outbounds\": [\n"
            + "    { \"protocol\": \"freedom\", \"tag\": \"direct\" },\n"
            + "    { \"protocol\": \"blackhole\", \"tag\": \"block\" }\n"
            + "  ]\n"
            + "}\n";
    }

    static void fetchToFile(String urlStr, Path dest) throws IOException {
        String url = urlStr;
        for (int redirects = 0; redirects <= 5; redirects++) {
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(120000);
            c.setReadTimeout(120000);
            c.setInstanceFollowRedirects(false);
            c.setRequestProperty("User-Agent", "wispbyte-xray-ws-skill/1.0");
            int code = c.getResponseCode();
            if (code >= 300 && code < 400) {
                String loc = c.getHeaderField("Location");
                c.disconnect();
                if (loc == null) throw new IOException("redirect without Location");
                url = loc;
                continue;
            }
            if (code != 200) {
                c.disconnect();
                throw new IOException("download HTTP " + code + " for " + url);
            }
            try (InputStream in = c.getInputStream()) {
                Files.copy(in, dest, StandardCopyOption.REPLACE_EXISTING);
            } finally {
                c.disconnect();
            }
            return;
        }
        throw new IOException("too many redirects");
    }

    static void downloadWithRetry(String url, Path dest, int attempts) throws IOException {
        IOException last = null;
        for (int n = 1; n <= attempts; n++) {
            try {
                System.out.println("downloading xray-core, attempt " + n);
                fetchToFile(url, dest);
                return;
            } catch (IOException e) {
                last = e;
                System.err.println(TAG + " download failed attempt " + n + ": " + e.getMessage());
                try { Files.deleteIfExists(dest); } catch (IOException ignored) {}
            }
        }
        throw last != null ? last : new IOException("download failed");
    }

    /** Extract the `xray` entry from the zip; returns extracted byte count. */
    static long extractXray(Path zip, Path dest) throws IOException {
        try (InputStream in = Files.newInputStream(zip);
             ZipInputStream zin = new ZipInputStream(in)) {
            ZipEntry e;
            while ((e = zin.getNextEntry()) != null) {
                String base = e.getName();
                int slash = base.lastIndexOf('/');
                if (slash >= 0) base = base.substring(slash + 1);
                if (base.equals("xray")) {
                    long bytes = Files.copy(zin, dest, StandardCopyOption.REPLACE_EXISTING);
                    try {
                        Set<PosixFilePermission> perms = new HashSet<>(Arrays.asList(
                            PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE,
                            PosixFilePermission.OWNER_EXECUTE, PosixFilePermission.GROUP_READ,
                            PosixFilePermission.GROUP_EXECUTE, PosixFilePermission.OTHERS_READ,
                            PosixFilePermission.OTHERS_EXECUTE));
                        Files.setPosixFilePermissions(dest, perms);
                    } catch (Exception ignored) {
                        // non-POSIX fs fallback
                        dest.toFile().setExecutable(true);
                    }
                    return bytes;
                }
            }
        }
        throw new IOException("xray entry not found in zip");
    }

    static void ensureXray(Path bin, Path zip, String zipUrl) throws IOException {
        if (Files.exists(bin)) {
            try { bin.toFile().setExecutable(true); } catch (Exception ignored) {}
            long size = Files.size(bin);
            if (size > 1000000) {
                System.out.println(TAG + " reusing cached xray, bytes: " + size);
                return;
            }
        }
        downloadWithRetry(zipUrl, zip, 3);
        long bytes = extractXray(zip, bin);
        System.out.println("xray extracted, bytes: " + bytes);
        try { Files.deleteIfExists(zip); } catch (IOException ignored) {}
    }

    public static void main(String[] args) throws Exception {
        String portStr = env("SERVER_PORT", env("PORT", "8080"));
        int port;
        try {
            port = Integer.parseInt(portStr.trim());
        } catch (NumberFormatException e) {
            die("invalid PORT/SERVER_PORT: " + portStr);
            return;
        }
        if (port <= 0) die("invalid PORT/SERVER_PORT: " + portStr);

        String uuid = env("UUID", "CHANGE_ME");
        if (uuid.equals("CHANGE_ME")) {
            System.out.println(TAG + " WARNING: UUID is still CHANGE_ME - set env UUID before real use");
        }
        String rawPath = env("WS_PATH", "/tk-rail");
        String wsPath = rawPath.startsWith("/") ? rawPath : "/" + rawPath;
        String loglevel = env("XRAY_LOGLEVEL", "warning");
        String zipUrl = env("XRAY_ZIP_URL",
            "https://github.com/XTLS/Xray-core/releases/download/v26.10.21/Xray-linux-64.zip");

        Path here = Paths.get("").toAbsolutePath();
        Path bin = here.resolve("xray");
        Path config = here.resolve("xray-config.json");
        Path zip = here.resolve("xray-download.zip");

        Files.write(config, buildConfig(port, uuid, wsPath, loglevel).getBytes("UTF-8"));
        System.out.println("starting xray on port " + port + " path " + wsPath);

        ensureXray(bin, zip, zipUrl);

        ProcessBuilder pb = new ProcessBuilder(bin.toString(), "run", "-c", config.toString());
        pb.inheritIO();
        final Process child = pb.start();
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try { child.destroy(); } catch (Exception ignored) {}
        }));
        int code = child.waitFor();
        System.err.println(TAG + " xray exited code=" + code);
        System.exit(code == 0 ? 1 : code);
    }
}
