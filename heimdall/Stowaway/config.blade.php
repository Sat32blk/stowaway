<h2>{{ __('app.apps.config') }} ({{ __('app.optional') }}) @include('items.enable')</h2>
<div class="items">
    <div class="input">
        <label>{{ strtoupper(__('app.url')) }}</label>
        {!! Form::text('config[override_url]', isset($item) ? $item->getconfig()->override_url : null, ['placeholder' => 'Leave empty when the tile URL is the app\'s Stowaway link', 'id' => 'override_url', 'class' => 'form-control']) !!}
    </div>
    <div class="input">
        <label>App name</label>
        {!! Form::text('config[app]', isset($item) ? ($item->getconfig()->app ?? null) : null, ['placeholder' => 'Only if the URL is the Stowaway dashboard', 'data-config' => 'app', 'class' => 'form-control config-item']) !!}
    </div>
    <div class="input">
        <button style="margin-top: 32px;" class="btn test" id="test_config">Test</button>
    </div>
    {{-- State changes slowly; refresh on the 30s "data only" cadence. --}}
    {!! Form::hidden('config[dataonly]', '1') !!}
</div>
