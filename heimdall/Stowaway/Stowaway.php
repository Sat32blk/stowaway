<?php

namespace App\SupportedApps\Stowaway;

/**
 * Stowaway puts Docker containers to sleep when they're idle and wakes them
 * when someone opens their link. This tile shows the app's status (In Use,
 * Sleeping in 8 min, Ready to Sleep, Sleeping...), using Stowaway's public status address
 * (which never wakes the app).
 *
 * Set the tile's URL to the app's Stowaway link (e.g. http://192.168.1.2:18096).
 * Nothing else is needed. Alternatively, put the Stowaway dashboard address in
 * the config URL and the app's name in "App name".
 */
class Stowaway extends \App\SupportedApps implements \App\EnhancedApps
{
    public $config;

    private const LABELS = [
        "running" => "Awake",
        "sleeping" => "Asleep",
        "starting" => "Waking",
        "stopping" => "Going to sleep",
        "updating" => "Updating",
        "maintenance" => "Maintenance",
        "failed" => "Failed",
        "stopped" => "Stopped",
    ];

    public function __construct()
    {
    }

    public function test()
    {
        $test = parent::appTest($this->url());
        echo $test->status;
    }

    public function livestats()
    {
        $status = "inactive";
        $data = ["state" => "Unknown", "color" => "#909296"];

        $response = parent::execute($this->url());
        if ($response !== null && $response->getStatusCode() === 200) {
            $details = json_decode($response->getBody(), true);
            if (is_array($details) && isset($details["state"])) {
                $state = $details["state"];
                // Stowaway 1.5+ sends a ready-made label ("In Use", "Sleeping in 8 min",
                // "Ready to Sleep", "Sleeping"...) and its colour.
                $data["state"] = $details["indicator"] ?? (self::LABELS[$state] ?? ucfirst($state));
                if (isset($details["indicator_hex"]) && preg_match('/^#[0-9a-fA-F]{6}$/', $details["indicator_hex"])) {
                    $data["color"] = $details["indicator_hex"];
                }
                // Refresh more often while something is changing.
                if (in_array($state, ["starting", "stopping", "updating", "maintenance"], true)) {
                    $status = "active";
                }
            }
        }

        return parent::getLiveStats($status, $data);
    }

    public function url($endpoint = "")
    {
        $base = (string) ($this->config->url ?? "");
        $base = preg_replace('#/_stowaway/?$#', '', rtrim($base, '/'));
        $name = trim((string) ($this->config->app ?? ""));
        if ($name === "") {
            $name = "this";
        }

        return parent::normaliseurl($base) . "_stowaway/status/" . rawurlencode($name);
    }
}
