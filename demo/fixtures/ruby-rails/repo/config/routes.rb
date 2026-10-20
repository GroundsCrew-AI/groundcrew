Rails.application.routes.draw do
  get "up" => "rails/health#show", as: :rails_health_check

  resources :orders, only: %i[index show create]
end
