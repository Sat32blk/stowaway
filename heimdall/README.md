# Stowaway tile for Heimdall

An "enhanced app" for [Heimdall](https://github.com/linuxserver/Heimdall). A tile shows:

- **Status:** Awake, Asleep, Switched off, Waking, Updating or Maintenance.
- **Restart:** when its next scheduled restart is.

It reads Stowaway's status address, which never wakes the app.

## Using it

Once it's part of Heimdall's app list:

1. Add an application tile and choose **Stowaway** as the application type.
2. Set the tile's **URL** to the app's Stowaway link, e.g. `http://192.168.1.2:18096`. Clicking the tile wakes and opens the app as usual.
3. Under **Config**, tick the switch to enable it and click **Test**. Nothing else is needed.

Alternatively, put the Stowaway dashboard address (`http://192.168.1.2:8880`) in the config URL and the app's name in **App name**. This also works for containers that only have a restart schedule.

## Adding it to Heimdall

**The easy way:** in the Stowaway dashboard, open **Integrations → Heimdall** and click **Add the Stowaway tile to heimdall**. Stowaway copies the files into Heimdall and registers the tile. This works with the linuxserver Heimdall image, which keeps the tile on its `/config` volume, so it stays through Heimdall updates. Reload Heimdall in your browser afterwards.

**By hand:** these steps assume the linuxserver Heimdall container is named `heimdall`.

```bash
docker cp heimdall/Stowaway heimdall:/app/www/app/SupportedApps/Stowaway
docker exec heimdall php /app/www/artisan register:app Stowaway
```

With the linuxserver image the tile is stored under Heimdall's `/config`, so it survives updates. If it ever disappears, repeat the two commands (or click the button again).

## Submitting it to Heimdall

Heimdall downloads its app list from [linuxserver/Heimdall-Apps](https://github.com/linuxserver/Heimdall-Apps). To make the tile available to everyone:

1. Fork Heimdall-Apps.
2. Copy this `Stowaway` folder into the root of the fork.
3. Open a pull request.

The folder follows the same layout as other enhanced apps: `Stowaway.php`, `app.json`, `config.blade.php`, `livestats.blade.php` and the icon. Before submitting, change the `website` in `app.json` to your published repository.
